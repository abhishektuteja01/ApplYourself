#!/bin/bash
# The whole night, started by the LaunchAgent: `uv run night` (src/night.py) runs
#   discover -> score -> synonyms -> tailor -> apply_prep -> cleanup -> summary -> email
# and records each stage in logs/pipeline_<stamp>/run.json. Arguments pass through
# (--resume, --only STAGE, --dry-run); so do DRY_RUN, STAGE_LIMIT, SKIP_STAGES and
# STAGES_DIR. Nothing here submits.
# Not exec: launchd started this bash, and the Word and Mail grants follow it.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || { echo "FATAL: cannot cd to the repo" >&2; exit 1; }
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
UV="${UV:-$(command -v uv)}"; [[ -n "$UV" ]] || { echo "FATAL: uv not found on PATH" >&2; exit 127; }
"$UV" run night "$@"
