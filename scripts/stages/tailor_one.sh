#!/bin/bash
# Tailor one role: /tailor, then check the resume and its PDF; retry, then
# re-render from the saved plan. Called by tailor.sh, one process per role.
# Appends "<job_id> ok|fail|deferred" to $RUN_DIR/tailor_results.txt (deferred:
# the usage limit was hit, so no retry; the role stays New for the next night).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
STAGE=tailor; LOG="$RUN_DIR/tailor.log"
ID="$1"
TOOLS='Bash(uv run *),Bash(cd *),Bash(git rev-parse *),Read,Write,Edit'

built() {  # 0 when the resume docx and pdf are both there
  "$UV" run python - "$ID" <<'PY'
import json, subprocess, sys
from pathlib import Path
info = json.loads(subprocess.run(["uv", "run", "app", "dir", sys.argv[1]],
                                 capture_output=True, text=True).stdout or "{}")
ok = info.get("has_resume") and (Path(info["dir"]) / f"{info['file_slug']}_Resume.pdf").is_file()
sys.exit(0 if ok else 1)
PY
}

RETRIES="$(cfg retries)" || exit 2
if [[ "$DRY_RUN" == 1 ]]; then
  run_claude "$ID" "$TOOLS" "/tailor $ID"
  echo "[dry-run] check has_resume + PDF; retry up to $RETRIES; else uv run app build resume $ID"
  exit 0
fi
RESULT=fail
for attempt in $(seq 0 "$RETRIES"); do
  run_claude "$ID" "$TOOLS" "/tailor $ID"; RC=$?
  [[ $RC -eq 0 ]] || echo "tailor $ID attempt $attempt failed"
  built && { echo "$ID ok" >> "$RUN_DIR/tailor_results.txt"; exit 0; }
  [[ $RC -eq 5 ]] && { RESULT=deferred; break; }
done
"$UV" run app build resume "$ID" >>"$LOG" 2>&1 && built \
  && { echo "$ID ok" >> "$RUN_DIR/tailor_results.txt"; exit 0; }
echo "$ID $RESULT" >> "$RUN_DIR/tailor_results.txt"
[[ $RESULT == deferred ]]
