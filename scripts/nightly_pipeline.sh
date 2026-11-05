#!/bin/bash
# The whole night, one stage after the next, started by the LaunchAgent:
#   discover -> score -> synonyms -> tailor -> apply_prep -> summary
# A failed discover or score stops the chain (everything after reads their
# output); a failed synonyms, tailor or apply_prep is recorded and the chain goes
# on. The summary always runs and says what failed. Nothing here submits.
#
#   DRY_RUN=1        print what would run, call nothing
#   STAGE_LIMIT=N    cap the roles tailor and apply_prep take (a first live test)
#   SKIP_STAGES="a b" leave those stages out (for example "discover" to reuse the
#                    scrape already done today); the summary shows them as not run
#   STAGES_DIR=D     run the stage scripts from D (tests)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
export RUN_DIR="$HERE/../logs/pipeline_$STAMP"   # before the lib, which would invent one
source "$HERE/lib_pipeline.sh"
RUN_DIR="$REPO/logs/pipeline_$STAMP"; export RUN_DIR

STAGES_DIR="${STAGES_DIR:-$REPO/scripts/stages}"
echo "=== pipeline start $STAMP (run dir $RUN_DIR dry_run=$DRY_RUN) ===" | tee -a "$RUN_DIR/pipeline.log"

# Keep log_keep_days (profile/discovery.yaml) of run folders; a config that
# fails to load skips the prune.
KEEP="$("$UV" run python -c 'from src.discovery.config import load_config; print(load_config().log_keep_days)' 2>/dev/null)"
if [[ "$KEEP" =~ ^[0-9]+$ && "$DRY_RUN" != 1 ]]; then
  find "$REPO/logs" -maxdepth 1 -type d -name 'pipeline_*' -mtime +"$KEEP" -exec rm -rf {} +
fi

# Hold off system sleep until this script exits, however it ends (macOS only).
# -s only counts on AC power; -i (idle sleep) holds on battery too.
if command -v caffeinate >/dev/null 2>&1 && [[ "$DRY_RUN" != 1 ]]; then
  caffeinate -s -i -w $$ &
fi

STOPPED=""
for stage in discover score synonyms tailor apply_prep; do
  if [[ " ${SKIP_STAGES:-} " == *" $stage "* ]]; then
    echo "=== $stage skipped ===" | tee -a "$RUN_DIR/pipeline.log"
    continue
  fi
  "$STAGES_DIR/$stage.sh" >>"$RUN_DIR/pipeline.log" 2>&1
  CODE=$?
  if [[ $CODE -ne 0 && ( "$stage" == discover || "$stage" == score ) ]]; then
    STOPPED="$stage"
    echo "=== $stage failed (code $CODE): stopping the chain ===" | tee -a "$RUN_DIR/pipeline.log"
    break
  fi
done

"$STAGES_DIR/summary.sh" >>"$RUN_DIR/pipeline.log" 2>&1
# Best effort: mailing the summary never changes how the night ended.
[[ -x "$STAGES_DIR/email.sh" ]] && "$STAGES_DIR/email.sh" >>"$RUN_DIR/pipeline.log" 2>&1

# One line per night.
LINE="$STAMP"
while read -r name code secs rest; do
  [[ -n "${name:-}" ]] || continue
  ok=ok; [[ "$code" == "code=0" ]] || ok="FAIL($code)"
  [[ "$ok" == ok && "$rest" == status=partial* ]] && ok=partial
  LINE="$LINE | $name $ok ${secs#seconds=}s"
done < "$RUN_DIR/summary.txt"
[[ -z "$STOPPED" ]] || LINE="$LINE | stopped at $STOPPED"
[[ "$DRY_RUN" == 1 ]] || echo "$LINE" >> "$REPO/logs/pipeline_history.log"
echo "=== pipeline end $(now) ===" | tee -a "$RUN_DIR/pipeline.log"
echo "$LINE"
[[ -z "$STOPPED" ]]
