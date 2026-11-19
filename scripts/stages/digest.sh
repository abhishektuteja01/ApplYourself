#!/bin/bash
# Digest stage: /night-digest turns the summary's digest_input.json into the
# short emailed digest.md (and .html). Best effort: no digest, the email sends
# summary.md.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start digest

rm -f "$RUN_DIR/digest.md" "$RUN_DIR/digest.html"
if [[ "$DRY_RUN" != 1 && ! -f "$RUN_DIR/digest_input.json" ]]; then
  stage_end 1 "no digest_input.json: the email sends summary.md"; exit 1
fi
run_claude night "Read,Write" "/night-digest $RUN_DIR" --disallowedTools Bash; RC=$?
if [[ "$DRY_RUN" == 1 ]]; then stage_end 0 "dry-run"; exit 0; fi
[[ $RC -eq 0 && -s "$RUN_DIR/digest.md" ]] || { stage_end 1 "claude wrote no digest (code $RC)"; exit 1; }
"$UV" run ay night summary --render "$RUN_DIR/digest.md" || { stage_end 1 "render failed"; exit 1; }
stage_end 0 ""
