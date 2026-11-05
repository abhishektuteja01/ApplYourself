#!/bin/bash
# Email stage: mail summary.html (summary.md as the plain-text part) to
# `summary_email` in profile/automation.yaml, through the Mail app. Best effort:
# a failure is logged and never stops anything. No address set: nothing is sent.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib_pipeline.sh"
stage_start email

ADDR="$(cfg summary_email 2>/dev/null)" || { stage_end 0 "no summary_email set: not sent"; exit 0; }
if [[ "$DRY_RUN" == 1 ]]; then MD="$RUN_DIR/summary.md"; else MD="$REPO/summary.md"; fi
HTML="${MD%.md}.html"
[[ -f "$MD" && -f "$HTML" ]] || { stage_end 1 "no summary to send"; exit 1; }

DATE="$(head -1 "$MD" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)"
FAILED="$(grep -cE '^\*\*[a-z_]+ failed\*\*' "$MD")"
PARTIAL="$(grep -cE '^\*\*[a-z_]+ partial\*\*' "$MD")"
if [[ "$FAILED" -gt 0 ]]; then SUBJECT="Night summary $DATE: $FAILED stage(s) failed"
elif [[ "$PARTIAL" -gt 0 ]]; then SUBJECT="Night summary $DATE: partial ($PARTIAL stage(s))"
else SUBJECT="Night summary $DATE: ok"; fi

if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] mail to $ADDR | $SUBJECT | $HTML"
  stage_end 0 "dry-run"; exit 0
fi
osascript - "$ADDR" "$SUBJECT" "$HTML" "$MD" <<'OSA'
on run argv
    set addr to item 1 of argv
    set subj to item 2 of argv
    set htmlText to read (POSIX file (item 3 of argv)) as «class utf8»
    set plainText to read (POSIX file (item 4 of argv)) as «class utf8»
    tell application "Mail"
        set m to make new outgoing message with properties {subject:subj, content:plainText, visible:false}
        try
            set html content of m to htmlText
        end try
        tell m to make new to recipient at end of to recipients with properties {address:addr}
        send m
    end tell
end run
OSA
CODE=$?
stage_end "$CODE" "$SUBJECT"
exit "$CODE"
