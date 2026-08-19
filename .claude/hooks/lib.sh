#!/usr/bin/env bash
# Shared helpers for this repo's Claude Code hooks.
#
# Every hook here is fail-open on a missing toolchain: a fresh clone that has
# not run `uv sync` yet, or a checkout outside git, must not have its edits
# blocked by a gate that cannot actually run. The gates that CAN run are
# fail-closed (exit 2 blocks and reports back).

# Read the hook's JSON payload from stdin once, into HOOK_JSON.
read_hook_json() {
  HOOK_JSON="$(cat)"
}

# Pull one top-level-or-nested string field out of HOOK_JSON.
#   hook_field tool_input file_path
#   hook_field '' stop_hook_active
hook_field() {
  local parent="$1" key="$2"
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$HOOK_JSON" | python3 -c '
import json, sys
parent, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if parent:
    data = data.get(parent) or {}
if isinstance(data, dict):
    value = data.get(key, "")
    sys.stdout.write("" if value is None else str(value))
' "$parent" "$key" 2>/dev/null
  else
    # No python3 at all. Degrade to a tolerant regex rather than guessing.
    printf '%s' "$HOOK_JSON" |
      sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" |
      head -1
  fi
}

# Repo root, or empty when this is not a git checkout.
repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || true
}

# Make the path relative to the repo root, so matching is stable whether the
# tool reported an absolute or a relative path.
relative_to_repo() {
  local path="$1" root="$2"
  [ -n "$path" ] || return 0
  case "$path" in
    "$root"/*) printf '%s' "${path#"$root"/}" ;;
    /*) printf '%s' "$path" ;;
    *) printf '%s' "$path" ;;
  esac
}
