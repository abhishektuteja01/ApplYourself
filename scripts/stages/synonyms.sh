#!/bin/bash
# Synonyms stage: writes the keywords of the roles with no resume yet to
# jobs/synonyms_keywords.json; /suggest-synonyms --auto reads them and writes
# jobs/synonyms_proposals.yaml; `uv run synonyms apply` checks and writes them.
# Nothing waits for a person: what needs one goes to summary.md (declined
# keywords, unlock questions).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start synonyms

TOOLS='Read,Write'
NO_BASH=(--disallowedTools Bash)  # the command's own allowed-tools would add Bash
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] uv run apply sheet --add"
  echo "[dry-run] uv run synonyms keywords > jobs/synonyms_keywords.json"
  run_claude auto "$TOOLS" "/suggest-synonyms --auto" "${NO_BASH[@]}"
  echo "[dry-run] uv run synonyms apply  (no proposals file fails the stage)"
  stage_end 0 "dry-run"; exit 0
fi

"$UV" run apply sheet --add >/dev/null || { stage_end 1 "apply sheet --add failed"; exit 1; }
KW="$("$UV" run synonyms keywords)" || { stage_end 1 "synonyms keywords failed"; exit 1; }
read -r ROLES NKW < <("$UV" run python -c 'import json,sys; d=json.load(sys.stdin); print(len(d["job_ids"]), len(d["keywords"]))' <<< "$KW")
[[ "${ROLES:-}" =~ ^[0-9]+$ && "${NKW:-}" =~ ^[0-9]+$ ]] || { stage_end 1 "counting roles and keywords failed"; exit 1; }
if [[ "$ROLES" -eq 0 ]]; then stage_end 0 "no new roles"; exit 0; fi
if [[ "$NKW" -eq 0 ]]; then stage_end 0 "$ROLES roles, no new keywords"; exit 0; fi
echo "New roles: $ROLES"
printf '%s\n' "$KW" > jobs/synonyms_keywords.json || { stage_end 1 "writing synonyms_keywords.json failed"; exit 1; }

rm -f jobs/synonyms_proposals.yaml
run_claude auto "$TOOLS" "/suggest-synonyms --auto" "${NO_BASH[@]}"; RC=$?
if [[ $RC -eq 5 ]]; then stage_end 1 "usage limit hit: the next run proposes them"; exit 1; fi
[[ $RC -eq 0 ]] || { stage_end 1 "claude failed (see ${STAGE}_auto.json)"; exit 1; }

# The session only writes the proposals; apply checks, writes and removes them.
[[ -f jobs/synonyms_proposals.yaml ]] || { stage_end 1 "no proposals written (see ${STAGE}_auto.json)"; exit 1; }
"$UV" run synonyms apply || { stage_end 1 "synonyms apply failed"; exit 1; }
ADDED="$("$UV" run synonyms added)"
stage_end 0 "$ROLES roles, $ADDED synonyms added today"
