#!/usr/bin/env bash
# /apply gate: a `<answers>.confirmed` file is the user's typed yes, written only
# by `uv run apply confirm` on their own terminal. A session that writes one
# approves its own answers, so Edit/Write on it, and Bash that writes it, are
# blocked. Reading it stays allowed. Pattern-based, so a soft guard: a path
# built indirectly can get past it.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

block() {
  {
    echo "BLOCKED -- $1"
    echo
    echo "A .confirmed file is the user's approval. Only the user writes it, by"
    echo "running \`uv run apply confirm <answers file>\` in their own terminal."
  } >&2
  exit 2
}

read_hook_json
FILE="$(hook_field tool_input file_path)"
case "$FILE" in
  *.confirmed) block "do not edit $FILE directly." ;;
esac

CMD="$(hook_field tool_input command)"
[ -n "$CMD" ] || exit 0

S='[^;|&]*'          # the rest of one command, up to the next separator
END="(['\"]|[[:space:]]|\$|[;|&)])"
has() { printf '%s' "$CMD" | grep -Eq -- "$1"; }

if has "confirmation\.(record|digest)|from[[:space:]]+src\.apply\.confirmation[[:space:]]+import${S}\b(record|digest)\b"; then
  block "this command calls the confirmation writer."
fi

if has "\b(python3?|uv)\b" && has "sha256" && has "sort_keys"; then
  block "this command recomputes a confirmation hash."
fi

if has "\.confirmed${END}|SIDECAR_SUFFIX|sidecar\("; then
  if has ">>?[[:space:]]*['\"]?[^[:space:];|&'\"]*\.confirmed${END}" ||
    has "\btee\b${S}\.confirmed${END}" ||
    has "\b(cp|mv|install|ln|rsync)\b${S}\.confirmed['\"]?[[:space:]]*(\$|[;|&)])" ||
    has "\b(touch|truncate)\b${S}\.confirmed${END}" ||
    has "\b(sed|perl)\b${S}[[:space:]]-i${S}\.confirmed${END}" ||
    has "\bof=[^[:space:]]*\.confirmed${END}"; then
    block "this command writes a .confirmed file."
  fi
  if has "\b(python3?|perl|ruby|node)\b" &&
    has "write_text|write_bytes|\.write\(|open\(${S},[[:space:]]*(mode[[:space:]]*=[[:space:]]*)?['\"][wax+]|\brename\(|\breplace\(|\.touch\(|shutil\.|\bdump\("; then
    block "this command writes a .confirmed file."
  fi
fi
exit 0
