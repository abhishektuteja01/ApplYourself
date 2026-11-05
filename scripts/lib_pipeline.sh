#!/bin/bash
# Shared helpers for the overnight stage scripts. Source it; do not run it.
#
# DRY_RUN=1  prints what would run and calls neither `claude` nor any uv
#            command that writes.
# RUN_DIR    the night's log folder (logs/pipeline_<stamp>); made if unset.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
UV="${UV:-$(command -v uv || true)}"
CLAUDE="${CLAUDE:-$(command -v claude || true)}"
DRY_RUN="${DRY_RUN:-0}"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO" >&2; exit 1; }
if [[ -z "$UV" ]]; then echo "FATAL: uv not found on PATH" >&2; exit 127; fi

if [[ -z "${RUN_DIR:-}" ]]; then
  RUN_DIR="$REPO/logs/pipeline_$(date +%Y-%m-%d_%H%M%S)"
fi
mkdir -p "$RUN_DIR"
export RUN_DIR DRY_RUN

# cfg KEY: one value from profile/automation.yaml. A missing key is an error,
# so the numbers live in the config, not here.
cfg() {
  "$UV" run python - "$1" <<'PY'
import sys, yaml
from src.shared import paths
data = yaml.safe_load((paths.PROFILE / "automation.yaml").read_text(encoding="utf-8")) or {}
key = sys.argv[1]
if key not in data:
    sys.exit(f"automation.yaml has no `{key}`")
print(data[key])
PY
}

now() { date +%Y-%m-%d_%H%M%S; }

notify() {  # notify TITLE MESSAGE
  local title="$1" msg="$2"
  echo "[notify] $title: $msg"
  if [[ "$DRY_RUN" != 1 ]] && command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${msg//\"/}\" with title \"${title//\"/}\"" \
      >/dev/null 2>&1 || true
  fi
}

# stage_start NAME: log to $RUN_DIR/NAME.log (and the terminal), announce.
stage_start() {
  STAGE="$1"
  STAGE_T0=$SECONDS
  LOG="$RUN_DIR/$STAGE.log"
  exec > >(tee -a "$LOG") 2>&1
  echo "=== $STAGE start $(now) (repo=$REPO dry_run=$DRY_RUN) ==="
  notify "Pipeline: $STAGE" "started"
}

# pdf_helper_up: start src/pdf_broker.py for this stage, so Word conversions run
# under this bash, not under a headless claude (a new app to macOS on every
# update). Stops when the stage exits. No helper: conversions run in-process.
pdf_helper_up() {
  [[ "$DRY_RUN" == 1 || -n "${PDF_BROKER_DIR:-}" ]] && return 0
  local dir="$RUN_DIR/pdf_helper_$STAGE" i
  mkdir -p "$dir"
  "$UV" run python -m src.pdf_broker "$dir" --parent $$ >>"$RUN_DIR/pdf_helper.log" 2>&1 &
  PDF_HELPER_PID=$!
  trap 'kill "$PDF_HELPER_PID" 2>/dev/null' EXIT
  for i in $(seq 1 150); do
    [[ -s "$dir/broker.pid" ]] && { export PDF_BROKER_DIR="$dir"; echo "pdf helper up ($dir)"; return 0; }
    sleep 0.2
  done
  kill "$PDF_HELPER_PID" 2>/dev/null
  echo "pdf helper did not start: Word runs from each claude session"
}

# stage_end CODE [DETAIL]: the last line of the log, one line in summary.txt.
stage_end() {
  local code="$1" detail="${2:-}" secs=$((SECONDS - STAGE_T0))
  echo "=== $STAGE end code=$code seconds=$secs $(now) ==="
  echo "$STAGE code=$code seconds=$secs $detail" >> "$RUN_DIR/summary.txt"
  if [[ "$code" -eq 0 ]]; then
    notify "Pipeline: $STAGE" "done ${detail}"
  else
    notify "Pipeline: $STAGE FAILED" "code=$code ${detail}"
  fi
}

# run_claude TAG TOOLS PROMPT [extra claude flags...]
# One headless call. The prompt is first and TOOLS is one comma-joined value,
# because --allowedTools takes a list and would swallow a prompt after it.
# The JSON result goes to $RUN_DIR/<stage>_<tag>.json; returns non-zero when the
# call failed or its result says is_error.
run_claude() {
  local tag="$1" tools="$2" prompt="$3"; shift 3
  local out="$RUN_DIR/${STAGE}_${tag}.json"
  local budget; budget="$(cfg max_budget_usd_per_call)" || return 2
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] claude -p \"$prompt\" --allowedTools \"$tools\" $* -> $out"
    return 0
  fi
  [[ -n "$CLAUDE" ]] || { echo "FATAL: claude not found on PATH" >&2; return 127; }
  "$CLAUDE" -p "$prompt" \
    --permission-mode dontAsk --permission-prompts none \
    --allowedTools "$tools" \
    --output-format json --no-session-persistence \
    --max-budget-usd "$budget" "$@" > "$out" 2>>"$LOG"
  local code=$?
  echo "claude $tag exit=$code -> $out"
  [[ $code -eq 0 ]] || return "$code"
  "$UV" run python - "$out" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(3)
if r.get("is_error"):
    print("claude reported is_error:", str(r.get("result"))[:300])
    sys.exit(4)
PY
}
