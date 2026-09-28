"""`uv run score`: the deterministic halves of /score (R7: no LLM calls).

  start [--all] [--vertical V] [--job-id ID ...]
      Drop what the hard rules reject, stage the rest for the judges, print
      one `judge <slice>...` line per judge to spawn. Prints `done` (after
      writing the shortlist) when nothing needs a judge. With no flags and a
      run already open, prints only the judges still owed.
  finish
      Merge every answered slice, prune, write the shortlist. Exit 1 with the
      `judge ...` lines still owed when a slice is missing or malformed.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from src import paths, score_store, shortlist, verticals
from src.discovery.config import load_config
from src.score_filter import CountryRule, load_scoring

CLEAN = paths.CLEAN
SCORED = paths.SCORED
STAGING = paths.STAGING
PIPELINE = paths.PIPELINE
SHORTLIST = paths.SHORTLIST


def _shortlist(cfg) -> str:
    path, keepers = shortlist.write(SCORED, CLEAN, PIPELINE, SHORTLIST, cfg,
                                    date.today().isoformat())
    shown = path.relative_to(paths.REPO_ROOT) if path.is_relative_to(paths.REPO_ROOT) else path
    return f"shortlist={shown} keepers={keepers}"


def _cmd_start(args: argparse.Namespace) -> int:
    if not CLEAN.exists():
        print("ERROR: jobs/clean.parquet missing — run discovery first")
        return 1
    cfg = load_scoring()
    if args.vertical and args.vertical not in verticals.get_config().names:
        print(f"ERROR: unknown vertical {args.vertical!r}")
        return 1
    try:
        res = score_store.start(CLEAN, SCORED, STAGING, cfg, rescore=args.all,
                                vertical=args.vertical, job_ids=args.job_id,
                                pipeline_dir=PIPELINE)
    except score_store.RunOpen as e:
        print(f"ERROR: {e}")
        return 1
    dropped = " ".join(f"dropped_{k}={n}" for k, n in sorted(res["dropped"].items()))
    print(f"{'resumed' if res['resumed'] else 'judge'}={res['judge']} {dropped}".strip())
    if not res["lines"] and not res["resumed"]:
        print(f"done {_shortlist(cfg)}")
    for line in res["lines"]:
        print(line)
    return 0


def _cmd_finish(args: argparse.Namespace) -> int:
    cfg = load_scoring()
    rule = CountryRule.from_discovery(load_config())
    res = score_store.finish(CLEAN, SCORED, STAGING, PIPELINE, cfg, rule)
    print(f"merged={res['merged']} pruned={res['pruned']} {_shortlist(cfg)}")
    for name, errs in res["bad"].items():
        print(f"bad {name}: {'; '.join(errs[:3])}")
    for line in res["lines"]:
        print(line)
    return 1 if res["lines"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="score", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("start", help="drop, stage, print judge lines")
    p.add_argument("--all", action="store_true", help="re-judge every row not past saved/tailored, not just new ones")
    p.add_argument("--vertical", metavar="V", help="only this lane")
    p.add_argument("--job-id", metavar="ID", action="append",
                   help="only these rows (repeatable); skips every drop except no-lane (/ingest)")
    p.set_defaults(func=_cmd_start)
    sub.add_parser("finish", help="check, merge, prune, write the shortlist").set_defaults(
        func=_cmd_finish)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
