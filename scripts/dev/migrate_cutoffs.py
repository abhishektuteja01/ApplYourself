"""Move the four score cutoffs into scoring.yaml `cutoffs:` (one-time).

  scoring.yaml     action.review_min, action.tailor_min -> cutoffs.review, cutoffs.tailor
  discovery.yaml   retention.score_threshold            -> cutoffs.keep_long
  automation.yaml  score_threshold                      -> cutoffs.sheet

Rewrites only those lines, so comments stay. --dry-run prints the diff;
--apply writes. The numbers are carried over as they are.

  uv run python scripts/dev/migrate_cutoffs.py --dry-run [--profile DIR]
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from applyourself.shared.config_schema import Cutoffs, validate  # noqa: E402


def _block_end(lines: list[str], start: int) -> int:
    """Index after the indented block that starts below lines[start]."""
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _drop_key(text: str, key: str) -> str:
    """text without `key` inside the top-level `retention:` mapping (flow or block)."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not line.startswith("retention:"):
            continue
        if "{" in line:
            lines[i] = re.sub(rf"{key}:\s*[^,}}]+,\s*|,?\s*{key}:\s*[^,}}]+", "", line, count=1)
        else:
            end = _block_end(lines, i)
            lines[i:end] = [x for x in lines[i:end] if not re.match(rf"\s+{key}:", x)]
        break
    return "".join(lines)


def migrate(profile: Path) -> dict[Path, tuple[str, str]]:
    """{path: (old text, new text)} for every file that changes."""
    scoring, discovery, automation = (
        profile / "scoring.yaml",
        profile / "discovery.yaml",
        profile / "automation.yaml",
    )
    s_text = scoring.read_text(encoding="utf-8")
    s_data = yaml.safe_load(s_text) or {}
    if "cutoffs" in s_data and "action" not in s_data:
        return {}
    action = s_data.get("action") or {}
    d_text = discovery.read_text(encoding="utf-8") if discovery.exists() else ""
    a_text = automation.read_text(encoding="utf-8") if automation.exists() else ""
    retention = (yaml.safe_load(d_text) or {}).get("retention") or {}
    values = {
        "review": action.get("review_min"),
        "tailor": action.get("tailor_min"),
        "keep_long": retention.get("score_threshold"),
        "sheet": (yaml.safe_load(a_text) or {}).get("score_threshold"),
    }
    missing = [k for k, v in values.items() if v is None]
    if missing:
        raise SystemExit(f"cannot migrate: no old value for cutoffs.{', cutoffs.'.join(missing)}")
    validate(Cutoffs, values, scoring)

    lines = s_text.splitlines(keepends=True)
    start = next(i for i, x in enumerate(lines) if x.startswith("action:"))
    end = _block_end(lines, start)
    lines[start:end] = ["cutoffs:\n"] + [f"  {k}: {v}\n" for k, v in values.items()]
    out = {scoring: (s_text, "".join(lines))}
    if d_text:
        out[discovery] = (d_text, _drop_key(d_text, "score_threshold"))
    if a_text:
        new = re.sub(r"(?m)^score_threshold:.*\n", "", a_text, count=1)
        out[automation] = (a_text, new)
    return {p: t for p, t in out.items() if t[0] != t[1]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--profile", type=Path, default=REPO / "profile")
    args = ap.parse_args(argv)
    changes = migrate(args.profile)
    if not changes:
        print("nothing to migrate")
        return 0
    for path, (old, new) in changes.items():
        sys.stdout.writelines(
            difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(path), str(path))
        )
        if args.apply:
            path.write_text(new, encoding="utf-8")
    print("written" if args.apply else "dry run: nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
