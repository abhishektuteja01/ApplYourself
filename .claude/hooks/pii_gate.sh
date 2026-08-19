#!/usr/bin/env bash
# PII gate at commit time. The pre-push hook is the real backstop, but it fires
# after the commit exists -- and a commit is what stamps PII into a message,
# author or committer field. Catching it here means no rewrite is needed.
#
# scripts/pii_scan.sh reads the git INDEX, so a commit is the right moment: by
# then everything going in has been staged. `git commit -am` is the one gap --
# it stages at commit time, so this sees the pre-existing index only.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

read_hook_json
CMD="$(hook_field tool_input command)"
case "$CMD" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

ROOT="$(repo_root)"
[ -n "$ROOT" ] || exit 0
cd "$ROOT" || exit 0
[ -x scripts/pii_scan.sh ] || exit 0

# A fresh clone has no denylist. Fail-open with the scan's own warning rather
# than blocking every commit on a file the user has not written yet.
OUT="$(PII_SCAN_ALLOW_MISSING=1 ./scripts/pii_scan.sh 2>&1)"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  {
    echo "PII GATE FAILED -- do not commit."
    echo
    printf '%s\n' "$OUT" | tail -30
    echo
    echo "This repo is public. Unstage or scrub the hit, then commit again."
  } >&2
  exit 2
fi
exit 0
