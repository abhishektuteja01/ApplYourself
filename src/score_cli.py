"""Deterministic CLI plumbing for /score and /rescore (R7: no LLM calls).

Each subcommand wraps src.scoring_io so the slash-command markdown stays a
thin sequence of one-liners. Judging is never done here.

  prepare [--force-all]              recover leftover batches (merge+prune),
                                     dump, split by vertical; prints counts +
                                     one `range <vertical> <A>-<B>` line per
                                     100-row judge chunk
  render                             compute + write shortlist/<date>.md,
                                     asserting invariants inline
  dump [--vertical V] [--force-all]  clear staging, dump unscored rows,
                                     merge the auto-skip categories (out-of-
                                     lane + hard-ineligible + one per
                                     configured vertical)
  split                              route unscored.jsonl into per-vertical
                                     unscored_<vertical>.jsonl files
  check-coverage                     compare staged batch job_ids against the
                                     dump (exit 1 if gaps/dupes/strays)
  merge [--model M]                  merge staged batches + prune, delete
                                     consumed staging files on success
  shortlist-input                    write shortlist_input.json, print counts
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from src import verticals
from src.scoring_io import (
    auto_score_disqualified,
    auto_score_ineligible,
    auto_score_out_of_lane,
    compute_shortlist,
    dump_shortlist_input,
    dump_unscored,
    merge_scores_from_dir,
    prune_scored,
    render_shortlist_markdown,
)

log = logging.getLogger(__name__)

CLEAN = Path("jobs/clean.parquet")
SCORED = Path("jobs/scored.parquet")
STAGING = Path("jobs/scored.staging")
PIPELINE = Path("pipeline")
DEFAULT_MODEL = "claude-sonnet-5"

_STAGING_GLOBS = ("batch_*.json", "unscored*.jsonl", "auto_skip*.jsonl")


def clear_staging(staging: Path) -> None:
    for pattern in _STAGING_GLOBS:
        for f in staging.glob(pattern):
            f.unlink()


def split_by_vertical(unscored_path: Path) -> dict[str, int]:
    """Route unscored.jsonl lines into sibling unscored_<vertical>.jsonl files
    by each row's precomputed vertical field. Line order is preserved so
    judge-mode --range line numbers stay stable. Returns per-vertical counts."""
    names = verticals.get_config().names
    counts = dict.fromkeys(names, 0)
    outs = {v: (unscored_path.parent / f"unscored_{v}.jsonl").open("w")
            for v in names}
    try:
        with unscored_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                v = json.loads(line)["vertical"]
                if v not in outs:
                    raise ValueError(
                        f"unexpected vertical {v!r} in {unscored_path} — "
                        "dump_unscored should only route configured verticals here"
                    )
                outs[v].write(line)
                counts[v] += 1
    finally:
        for f in outs.values():
            f.close()
    return counts


def coverage(staging: Path) -> dict:
    """Compare job_ids staged in batch_*.json against the dump. Counting
    only — batch contents are never judged or rewritten here."""
    staged: list[str] = []
    for f in sorted(staging.glob("batch_*.json")):
        staged += [r["job_id"] for r in json.loads(f.read_text())]
    with (staging / "unscored.jsonl").open() as f:
        expected = [json.loads(line)["job_id"] for line in f if line.strip()]
    return {
        "staged": len(staged),
        "expected": len(expected),
        "missing": sorted(set(expected) - set(staged)),
        "unexpected": sorted(set(staged) - set(expected)),
        "dupes": len(staged) - len(set(staged)),
    }


def _cmd_dump(args: argparse.Namespace) -> int:
    STAGING.mkdir(parents=True, exist_ok=True)
    clear_staging(STAGING)
    n_judge = dump_unscored(
        CLEAN, SCORED, STAGING / "unscored.jsonl",
        force_all=args.force_all, only_vertical=args.vertical,
    )
    n_skip = auto_score_out_of_lane(STAGING / "auto_skip.jsonl", SCORED)
    n_ineligible = auto_score_ineligible(
        STAGING / "auto_skip_ineligible.jsonl", SCORED)
    per_vertical = {
        v.name: auto_score_disqualified(
            v, STAGING / f"auto_skip_{v.name}.jsonl", SCORED)
        for v in verticals.get_config().verticals.values()
    }
    print(f"rows_to_score={n_judge} auto_skipped={n_skip} "
          f"auto_ineligible={n_ineligible} "
          + " ".join(f"auto_skipped_{v}={n}" for v, n in per_vertical.items()))
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    counts = split_by_vertical(STAGING / "unscored.jsonl")
    print(" ".join(f"{v}={n}" for v, n in counts.items()))
    return 0


def _cmd_check_coverage(args: argparse.Namespace) -> int:
    report = coverage(STAGING)
    print(f"staged={report['staged']} expected={report['expected']}")
    print(f"missing: {report['missing']}")
    print(f"unexpected: {report['unexpected']}")
    print(f"dupes: {report['dupes']}")
    clean = (report["staged"] == report["expected"]
             and not report["missing"] and not report["unexpected"]
             and report["dupes"] == 0)
    return 0 if clean else 1


def _cmd_merge(args: argparse.Namespace) -> int:
    n_merged = merge_scores_from_dir(SCORED, STAGING, scored_by_model=args.model)
    n_pruned = prune_scored(SCORED, CLEAN)
    clear_staging(STAGING)
    print(f"merged={n_merged} pruned={n_pruned}")
    return 0


def _cmd_shortlist_input(args: argparse.Namespace) -> int:
    STAGING.mkdir(parents=True, exist_ok=True)
    dump_shortlist_input(
        SCORED, CLEAN, PIPELINE, STAGING / "shortlist_input.json",
        top_n=25, min_fit=50,
    )
    n_scored = len(pd.read_parquet(SCORED))
    n_clean = len(pd.read_parquet(CLEAN))
    print(f"n_scored={n_scored} n_clean={n_clean}")
    return 0


def judge_ranges(counts: dict[str, int], chunk: int = 100) -> list[tuple[str, int, int]]:
    """Chunk each vertical's row count into consecutive 1-indexed ranges of
    at most `chunk` rows (last may be short). Verticals with count 0 emit no
    range."""
    out: list[tuple[str, int, int]] = []
    for v, n in counts.items():
        start = 1
        while start <= n:
            end = min(start + chunk - 1, n)
            out.append((v, start, end))
            start = end + 1
    return out


def _cmd_prepare(args: argparse.Namespace) -> int:
    if not CLEAN.exists():
        print("ERROR: jobs/clean.parquet missing — run discovery first")
        return 1
    sponsorship_rules = Path("profile/sponsorship_rules.yaml")
    if not sponsorship_rules.exists():
        print(f"ERROR: {sponsorship_rules} missing")
        return 1
    try:
        cfg = verticals.get_config()
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    STAGING.mkdir(parents=True, exist_ok=True)
    recovered = 0
    if any(STAGING.glob("batch_*.json")):
        recovered = merge_scores_from_dir(SCORED, STAGING, scored_by_model=DEFAULT_MODEL)
        prune_scored(SCORED, CLEAN)

    clear_staging(STAGING)
    n_judge = dump_unscored(CLEAN, SCORED, STAGING / "unscored.jsonl",
                            force_all=args.force_all)
    n_skip = auto_score_out_of_lane(STAGING / "auto_skip.jsonl", SCORED)
    n_ineligible = auto_score_ineligible(
        STAGING / "auto_skip_ineligible.jsonl", SCORED)
    per_vertical = {
        v.name: auto_score_disqualified(
            v, STAGING / f"auto_skip_{v.name}.jsonl", SCORED)
        for v in cfg.verticals.values()
    }
    counts = (split_by_vertical(STAGING / "unscored.jsonl") if n_judge
              else dict.fromkeys(cfg.names, 0))

    print(f"recovered={recovered} rows_to_score={n_judge} auto_skipped={n_skip} "
          f"auto_ineligible={n_ineligible} "
          + " ".join(f"auto_skipped_{v}={n}" for v, n in per_vertical.items()))
    for v, a, b in judge_ranges(counts):
        print(f"range {v} {a}-{b}")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    from datetime import date

    cfg = verticals.get_config()
    result = compute_shortlist(SCORED, CLEAN, PIPELINE, top_n=25, min_fit=50)
    n_scored = len(pd.read_parquet(SCORED)) if SCORED.exists() else 0
    n_clean = len(pd.read_parquet(CLEAN))
    date_str = date.today().isoformat()
    md = render_shortlist_markdown(result, cfg, date_str, n_scored, n_clean)

    out_dir = Path("shortlist")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.md"
    out_path.write_text(md)

    keepers = sum(len(rows) for rows in result["main"].values())
    print(f"shortlist={out_path} keepers={keepers} scored={n_scored}/{n_clean}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="python -m src.score_cli",
                                     description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_prep = sub.add_parser("prepare", help="recover, dump, split; print judge ranges")
    p_prep.add_argument("--force-all", action="store_true",
                        help="ignore scored.parquet (used by /rescore)")
    p_prep.set_defaults(func=_cmd_prepare)

    p_render = sub.add_parser("render", help="write shortlist/<date>.md")
    p_render.set_defaults(func=_cmd_render)

    p_dump = sub.add_parser("dump", help="clear staging, dump unscored, merge auto-skips")
    p_dump.add_argument("--vertical", choices=verticals.get_config().names, default=None)
    p_dump.add_argument("--force-all", action="store_true",
                        help="ignore scored.parquet (used by /rescore)")
    p_dump.set_defaults(func=_cmd_dump)

    p_split = sub.add_parser("split", help="split unscored.jsonl by vertical")
    p_split.set_defaults(func=_cmd_split)

    p_cov = sub.add_parser("check-coverage", help="staged batches vs dump")
    p_cov.set_defaults(func=_cmd_check_coverage)

    p_merge = sub.add_parser("merge", help="merge staged batches, prune, clean staging")
    p_merge.add_argument("--model", default=DEFAULT_MODEL,
                         help="scored_by_model stamp (default: %(default)s)")
    p_merge.set_defaults(func=_cmd_merge)

    p_sl = sub.add_parser("shortlist-input", help="write shortlist_input.json")
    p_sl.set_defaults(func=_cmd_shortlist_input)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
