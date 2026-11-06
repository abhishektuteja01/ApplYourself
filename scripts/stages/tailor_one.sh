#!/bin/bash
# Tailor one role: /tailor, then check the resume and its PDF; retry, then
# re-render from the saved plan. A docx with no PDF re-renders only (no model).
# Called by tailor.sh, one process per role.
# Appends "<job_id> ok|fail|deferred" to $RUN_DIR/tailor_results.txt (deferred:
# the usage limit was hit, so no retry; the role stays New for the rerun after the reset).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
# shellcheck disable=SC2034  # read by lib_pipeline.sh
STAGE=tailor; LOG="$RUN_DIR/tailor.log"
ID="$1"
TOOLS='Bash(uv run verticals-check *),Bash(uv run app *),Bash(cd *),Bash(git rev-parse *),Read,Write,Edit'

has() {  # built: the newest folder's build is recorded, with its pdf; pdf_missing: docx, no pdf
  "$UV" run python - "$ID" "$1" <<'PY'
import json, subprocess, sys
from pathlib import Path
info = json.loads(subprocess.run(["uv", "run", "app", "dir", sys.argv[1]],
                                 capture_output=True, text=True).stdout or "{}")
w = Path(info.get("work_dir") or "/nonexistent")
docx = (w / f"{info.get('file_slug')}_Resume.docx").is_file()
pdf = (w / f"{info.get('file_slug')}_Resume.pdf").is_file()
if sys.argv[2] == "built":
    ok = docx and pdf and info.get("dir") == info.get("work_dir")
else:
    ok = docx and not pdf
sys.exit(0 if ok else 1)
PY
}

RETRIES="$(cfg retries)" || { echo "$ID fail" >> "$RUN_DIR/tailor_results.txt"; exit 2; }
if [[ "$DRY_RUN" == 1 ]]; then
  run_claude "$ID" "$TOOLS" "/tailor $ID"
  echo "[dry-run] check build + PDF; docx without PDF: uv run app build resume $ID, else /tailor; retry up to $RETRIES"
  exit 0
fi
RESULT=fail
for attempt in $(seq 0 "$RETRIES"); do
  RC=0
  if has pdf_missing; then
    "$UV" run app build resume "$ID" >>"$LOG" 2>&1 || echo "rebuild $ID attempt $attempt failed"
  else
    run_claude "$ID" "$TOOLS" "/tailor $ID"; RC=$?
    [[ $RC -eq 0 ]] || echo "tailor $ID attempt $attempt failed"
  fi
  has built && { echo "$ID ok" >> "$RUN_DIR/tailor_results.txt"; exit 0; }
  [[ $RC -eq 5 ]] && { RESULT=deferred; break; }
done
"$UV" run app build resume "$ID" >>"$LOG" 2>&1 && has built \
  && { echo "$ID ok" >> "$RUN_DIR/tailor_results.txt"; exit 0; }
echo "$ID $RESULT" >> "$RUN_DIR/tailor_results.txt"
[[ $RESULT == deferred ]]
