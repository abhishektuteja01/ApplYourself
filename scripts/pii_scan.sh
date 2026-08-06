#!/usr/bin/env bash
# PII gate: fail if any denylisted string appears in a tracked file.
#
# Patterns come from profile/pii_denylist.txt, which is gitignored — the list of
# strings to keep out is itself the thing being kept out. Format and examples:
# profile/pii_denylist.example.txt.
#
# Exit: 0 clean (or no denylist on disk), 1 hits found, 2 misconfigured/grep error.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

denylist="profile/pii_denylist.txt"
example="profile/pii_denylist.example.txt"

# Tracked paths that carry denylisted strings on purpose. Edit deliberately.
#   LICENSE          — MIT attribution names the copyright holder by design.
#   data/universe/*  — vendored MIT company lists (real company names), and the
#                      source of every substring false positive seen so far.
allowlist=(
  LICENSE
  data/universe/ashby.csv
  data/universe/greenhouse.csv
  data/universe/lever.csv
)

if [[ ! -f "$denylist" ]]; then
  printf 'pii_scan: no %s on disk — nothing to check.\n' "$denylist"
  printf '          cp %s %s and fill it in.\n' "$example" "$denylist"
  exit 0
fi

patterns=$(mktemp)
trap 'rm -f "$patterns"' EXIT
grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$denylist" >"$patterns" || true

# An empty pattern file makes grep's behaviour implementation-defined; refuse to
# report a clean scan we did not actually run.
if [[ ! -s "$patterns" ]]; then
  printf 'pii_scan: %s has no patterns (comments/blanks only).\n' "$denylist" >&2
  exit 2
fi

scan_files=()
while IFS= read -r -d '' f; do
  skip=false
  for a in "${allowlist[@]}"; do
    if [[ "$f" == "$a" ]]; then skip=true; break; fi
  done
  if [[ "$skip" == false ]]; then scan_files+=("$f"); fi
done < <(git ls-files -z)

if ((${#scan_files[@]} == 0)); then
  printf 'pii_scan: no tracked files to scan.\n'
  exit 0
fi

# Patterns are extended regexes matched case-insensitively on whole words (-w),
# the same shape as the manual scan this replaces.
set +e
hits=$(grep -H -n -I -i -w -E -f "$patterns" -- "${scan_files[@]}")
status=$?
set -e

case "$status" in
  0)
    printf 'pii_scan: FAIL — denylisted strings in tracked files:\n\n%s\n\n' "$hits" >&2
    printf 'Remove them, or add a deliberate exception to the allowlist in %s.\n' "$0" >&2
    exit 1
    ;;
  1)
    printf 'pii_scan: OK — %d tracked files scanned, no hits.\n' "${#scan_files[@]}"
    ;;
  *)
    printf 'pii_scan: grep failed (exit %d) — treating as unscanned.\n' "$status" >&2
    exit 2
    ;;
esac
