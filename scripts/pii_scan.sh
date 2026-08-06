#!/usr/bin/env bash
# PII gate: fail if any denylisted string appears in a tracked file.
#
# Patterns come from profile/pii_denylist.txt, which is gitignored — the list of
# strings to keep out is itself the thing being kept out. Format and examples:
# profile/pii_denylist.example.txt.
#
# Scans INDEX content (git grep --cached), not the working tree, so what it
# reports is what a commit would carry: run it AFTER staging. Reading the working
# tree instead would pass a file whose PII is staged but since edited out, and
# would abort on a tracked file deleted from disk.
# It says nothing about history and nothing about unstaged work.
#
# Exit: 0 clean, 1 hits found, 2 misconfigured/grep error.
# Set PII_SCAN_ALLOW_MISSING=1 to downgrade a missing denylist to a warning.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

denylist="profile/pii_denylist.txt"
example="profile/pii_denylist.example.txt"

# Tracked paths that carry denylisted strings on purpose. Edit deliberately.
#   LICENSE          — MIT attribution names the copyright holder by design.
#   data/universe/*  — vendored MIT company lists (real company names), and the
#                      source of every substring false positive seen so far.
#   the example      — a file OF patterns; every pattern matches its own line,
#                      so without this no template can ever scan clean.
#   the two .example.docx — the ONLY tracked binaries. A Word template cannot be
#                      a text file, and the binary check below would otherwise
#                      reject them on sight. Narrowly allowlisted BY NAME, never
#                      by a *.docx glob, and guarded by
#                      tests/test_example_templates.py: it unzips both, asserts
#                      their text is confined to an approved set, and asserts
#                      empty core properties. They are hand-authored in Word;
#                      run scripts/scrub_example_templates.py after any save,
#                      which strips the name Word stamps into the metadata.
allowlist=(
  LICENSE
  "$example"
  data/universe/ashby.csv
  data/universe/greenhouse.csv
  data/universe/lever.csv
  profile/resume_template.example.docx
  profile/cover_letter_template.example.docx
)

in_allowlist() {
  local f=$1 a
  for a in "${allowlist[@]}"; do
    [[ "$f" == "$a" ]] && return 0
  done
  return 1
}

if [[ ! -f "$denylist" ]]; then
  # Fail closed. A missing or renamed denylist used to exit 0, which is a green
  # gate that never ran — the worst possible outcome for a gate.
  printf 'pii_scan: no %s on disk — the gate did not run.\n' "$denylist" >&2
  printf '          cp %s %s and fill it in,\n' "$example" "$denylist" >&2
  printf '          or set PII_SCAN_ALLOW_MISSING=1 if this repo has nothing to protect.\n' >&2
  [[ "${PII_SCAN_ALLOW_MISSING:-}" == 1 ]] || exit 2
  printf 'pii_scan: PII_SCAN_ALLOW_MISSING=1 — skipping.\n' >&2
  exit 0
fi

# Binary files are invisible to the text scan below (-I), so a staged .docx or
# .parquet would be counted as "scanned and clean". Refuse instead.
binary_re='\.(docx|doc|pdf|parquet|xlsx|xls|pptx|png|jpe?g|gif|ico|zip|tgz|gz|bz2|7z|bin|so|dylib|dll|pyc|woff2?|ttf|otf|mp4|mov)$'
staged_binaries=()
while IFS= read -r -d '' f; do
  if [[ "$f" =~ $binary_re ]] && ! in_allowlist "$f"; then
    staged_binaries+=("$f")
  fi
done < <(git ls-files -z)

if ((${#staged_binaries[@]} > 0)); then
  printf 'pii_scan: FAIL — tracked binary file(s) the text scan cannot read:\n\n' >&2
  printf '  %s\n' "${staged_binaries[@]}" >&2
  printf '\nA .docx/.pdf/.parquet can carry a name, an address or a whole résumé.\n' >&2
  printf 'Untrack it, or add it to the allowlist in %s deliberately.\n' "$0" >&2
  exit 1
fi

# Two pattern files: word-boundary matching is the default, and a leading "~"
# opts a pattern out of it. -w is why "abhishek" misses "abhishektuteja01" and
# why a phone pattern misses every alternative separator.
word_patterns=$(mktemp)
sub_patterns=$(mktemp)
trap 'rm -f "$word_patterns" "$sub_patterns"' EXIT
grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$denylist" \
  | sed -e 's/[[:space:]]*$//' \
  | awk -v w="$word_patterns" -v s="$sub_patterns" \
      '{ if (substr($0,1,1) == "~") { print substr($0,2) > s } else { print > w } }' || true

if [[ ! -s "$word_patterns" && ! -s "$sub_patterns" ]]; then
  printf 'pii_scan: %s has no patterns (comments/blanks only).\n' "$denylist" >&2
  exit 2
fi

scan_files=()
while IFS= read -r -d '' f; do
  in_allowlist "$f" || scan_files+=("$f")
done < <(git ls-files -z)

if ((${#scan_files[@]} == 0)); then
  printf 'pii_scan: no tracked files to scan.\n'
  exit 0
fi

# Patterns are extended regexes matched case-insensitively.
hits=""
scan() {  # scan <pattern-file> <extra-git-grep-flag...>
  local pf=$1; shift
  [[ -s "$pf" ]] || return 0
  local out status
  set +e
  out=$(git grep --cached -I -n -i -E "$@" -f "$pf" -- "${scan_files[@]}")
  status=$?
  set -e
  case "$status" in
    0) hits+="$out"$'\n' ;;
    1) ;;
    *) printf 'pii_scan: git grep failed (exit %d) — treating as unscanned.\n' "$status" >&2
       exit 2 ;;
  esac
}

scan "$word_patterns" -w
scan "$sub_patterns"

if [[ -n "${hits//[$'\n']/}" ]]; then
  printf 'pii_scan: FAIL — denylisted strings in tracked files:\n\n%s\n' "$hits" >&2
  printf 'Remove them, or add a deliberate exception to the allowlist in %s.\n' "$0" >&2
  exit 1
fi

printf 'pii_scan: OK — %d tracked files scanned, no hits.\n' "${#scan_files[@]}"
