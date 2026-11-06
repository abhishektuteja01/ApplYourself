"""`uv run score`: the deterministic halves of /score (R7: no LLM calls).

start [--all | --stale-only [--yes]] [--vertical V] [--job-id ID ...]
    Drop what the hard rules reject (never stored), stage the rest for the
    judges, print one `judge <slice>...` line per judge to spawn. Prints
    `done` (after writing the shortlist) when nothing needs a judge. With no
    flags and a run already open, prints only the judges still owed.
    --stale-only prints how many judged rows changed input and stages
    nothing; --yes stages them. A row with no input hash is never stale.
    `dropped_<code>=n` counts every drop, `filtered_<code>=n` only those
    the latest discovery run saw first (what the night summary shows).
finish
    Merge every answered slice, write the shortlist. Exit 1 with the
    `judge ...` lines still owed when a slice is missing or malformed.
show <job_id>
    One role's score as every reader sees it. Read-only. Exit 1 when the
    role has no score (never judged, or a filter drop).
lane <vertical>
    What a lane caught in clean.parquet, what the other lanes and no lane
    caught, and the lane's judged scores. Read-only (/tune-vertical).
"""

from __future__ import annotations

import argparse
import json
import sys

from src import score_store, shortlist
from src.discovery.config import load_config
from src.score_filter import CountryRule, ScoringConfig, load_scoring
from src.shared import clock, paths, verticals


def _shortlist(cfg: ScoringConfig) -> str:
    path, keepers = shortlist.write(
        paths.SCORED, paths.CLEAN, paths.PIPELINE, paths.SHORTLIST, cfg, clock.today().isoformat()
    )
    shown = path.relative_to(paths.root()) if path.is_relative_to(paths.root()) else path
    return f"shortlist={shown} keepers={keepers}"


def _cmd_start(args: argparse.Namespace) -> int:
    if not paths.CLEAN.exists():
        print("ERROR: jobs/clean.parquet missing — run discovery first")
        return 1
    cfg = load_scoring()
    if args.vertical and args.vertical not in verticals.get_config().names:
        print(f"ERROR: unknown vertical {args.vertical!r}")
        return 1
    if args.stale_only and (args.all or args.job_id):
        print("ERROR: --stale-only takes no --all or --job-id")
        return 1
    count_only = args.stale_only and not args.yes
    try:
        res = score_store.start(
            paths.CLEAN,
            paths.SCORED,
            paths.STAGING,
            cfg,
            rescore=args.all,
            stale_only=args.stale_only,
            count_only=count_only,
            vertical=args.vertical,
            job_ids=args.job_id,
            pipeline_dir=paths.PIPELINE,
            store_path=paths.STORE,
        )
    except score_store.RunOpen as e:
        print(f"ERROR: {e}")
        return 1
    dropped = " ".join(
        [f"dropped_{k}={n}" for k, n in sorted(res["dropped"].items())]
        + [f"filtered_{k}={n}" for k, n in sorted(res["first_dropped"].items())]
    )
    if count_only:
        print(f"stale={res['judge']} {dropped}".strip())
        print("done nothing staged: add --yes to stage the stale rows for judging")
        return 0
    print(f"{'resumed' if res['resumed'] else 'judge'}={res['judge']} {dropped}".strip())
    if not res["lines"] and not res["resumed"]:
        print(f"done {_shortlist(cfg)}")
    for line in res["lines"]:
        print(line)
    return 0


def _cmd_finish(args: argparse.Namespace) -> int:
    cfg = load_scoring()
    rule = CountryRule.from_discovery(load_config())
    res = score_store.finish(paths.SCORED, paths.STAGING, cfg, rule)
    print(f"merged={res['merged']} {_shortlist(cfg)}")
    for job_id, regex, judge in res["years_gaps"]:
        print(f"years_gap {job_id} regex={regex} judge={judge}")
    for name, errs in res["bad"].items():
        print(f"bad {name}: {'; '.join(errs[:3])}")
    for line in res["lines"]:
        print(line)
    return 1 if res["lines"] else 0


def _cmd_show(args: argparse.Namespace) -> int:
    v = score_store.view(paths.SCORED)
    rows = v[v["job_id"] == args.job_id]
    if rows.empty:
        print(f"not scored: {args.job_id}")
        return 1
    r = rows.iloc[0]
    subs = json.loads(r["fit_subscores"])
    print(
        f"fit_score={r['fit_score']:.0f} action={r['suggested_action']} "
        f"sponsorship={r['sponsorship_label']} basis={r['basis']}"
    )
    print(f"reasoning: {r['reasoning']}")
    print("subscores: " + " / ".join(f"{k} {n}" for k, n in subs.items()))
    print("keywords: " + ", ".join(map(str, r["keywords_to_mirror"])))
    print(f"sponsorship evidence: {r['sponsorship_evidence'] or '-'}")
    return 0


def _cmd_lane(args: argparse.Namespace) -> int:
    import pandas as pd

    lane = args.vertical
    d = pd.read_parquet(paths.CLEAN, columns=["job_id", "title", "vertical"])
    v = d[d["vertical"] == lane]
    print(f"== caught: {len(v)} in lane / {len(d)} total")
    print(v["title"].value_counts().head(40).to_string())
    print("== not caught: other lanes and out-of-lane titles")
    rest = d[d["vertical"] != lane]
    for name, g in rest.groupby(rest["vertical"].fillna("")):
        print("--", name or "(out-of-lane)", len(g))
        print(g["title"].value_counts().head(15).to_string())
    s = score_store.view(paths.SCORED)
    s = s[s["vertical"] == lane]
    print(f"== judged scores: {len(s)}")
    print(s["fit_score"].describe().to_string())
    top = s.sort_values("fit_score", ascending=False)
    print(top[["fit_score", "suggested_action", "keywords_to_mirror"]].head(15).to_string())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="score", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("start", help="drop, stage, print judge lines")
    p.add_argument(
        "--all",
        action="store_true",
        help="re-judge every row not past saved/tailored, not just new ones",
    )
    p.add_argument(
        "--stale-only",
        action="store_true",
        help="only judged rows whose JD or packet changed; prints the count, stages with --yes",
    )
    p.add_argument("--yes", action="store_true", help="with --stale-only: stage the stale rows")
    p.add_argument("--vertical", metavar="V", help="only this lane")
    p.add_argument(
        "--job-id",
        metavar="ID",
        action="append",
        help="only these rows (repeatable); skips every drop except no-lane (/ingest)",
    )
    p.set_defaults(func=_cmd_start)
    sub.add_parser("finish", help="check, merge, write the shortlist").set_defaults(
        func=_cmd_finish
    )
    p = sub.add_parser("show", help="one role's score, read-only")
    p.add_argument("job_id")
    p.set_defaults(func=_cmd_show)
    p = sub.add_parser("lane", help="one lane's caught titles, neighbours and scores, read-only")
    p.add_argument("vertical")
    p.set_defaults(func=_cmd_lane)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
