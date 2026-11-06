#!/bin/bash
# Cleanup stage: `track cleanup --apply` deletes the application folders of
# skip / withdrawn / rejected / ghosted roles, archiving the resume of those that
# were applied. DRY_RUN=1 runs it with --dry-run (counts only).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start cleanup

MODE=--apply; [[ "$DRY_RUN" == 1 ]] && MODE=--dry-run
OUT="$("$UV" run track cleanup "$MODE")"; CODE=$?
echo "$OUT"
stage_end "$CODE" "${OUT#OK: }"
exit "$CODE"
