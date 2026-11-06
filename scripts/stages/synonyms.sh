#!/bin/bash
# Synonyms stage: /suggest-synonyms --auto reads the keywords of the roles with
# no resume yet and writes jobs/synonyms_proposals.yaml; `uv run synonyms apply`
# checks and writes them. Nothing waits for a person: what needs one goes to
# summary.md (declined keywords, unlock questions).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start synonyms

TOOLS='Bash(uv run apply sheet *),Bash(uv run synonyms *),Bash(cd *),Bash(git rev-parse *),Read,Write'
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run apply sheet --add"
  run_claude auto "$TOOLS" "/suggest-synonyms --auto"
  echo "[dry-run] uv run synonyms apply  (only if the proposals file is still there)"
  stage_end 0 "dry-run"; exit 0
fi

"$UV" run apply sheet --add >/dev/null || { stage_end 1 "apply sheet --add failed"; exit 1; }
KW="$("$UV" run synonyms keywords)" || { stage_end 1 "synonyms keywords failed"; exit 1; }
ROLES="$("$UV" run python -c 'import json,sys; print(len(json.load(sys.stdin)["job_ids"]))' <<< "$KW")"
if [[ "$ROLES" -eq 0 ]]; then stage_end 0 "no new roles"; exit 0; fi
echo "New roles: $ROLES"

rm -f jobs/synonyms_proposals.yaml
run_claude auto "$TOOLS" "/suggest-synonyms --auto"; RC=$?
if [[ $RC -eq 5 ]]; then stage_end 1 "usage limit hit: the next run proposes them"; exit 1; fi
[[ $RC -eq 0 ]] || { stage_end 1 "claude failed (see ${STAGE}_auto.json)"; exit 1; }

# The session runs `synonyms apply` itself; if it did not, do it here.
if [[ -f jobs/synonyms_proposals.yaml ]]; then
  "$UV" run synonyms apply || { stage_end 1 "synonyms apply failed"; exit 1; }
fi
ADDED="$("$UV" run synonyms added)"
stage_end 0 "$ROLES roles, $ADDED synonyms added today"
