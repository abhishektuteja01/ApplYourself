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
    caught, and the lane's judged scores and cutoff. Read-only (/tune-vertical).
examples [--vertical V] [--write [--force]]
    Pick each lane's calibration examples from past scores by a stable rule
    and print them; --write saves profile/verticals/<lane>/examples.yaml
    (--force replaces one that exists).
ab-stage --out DIR [--rows N] [--top K] [--repeat R] [--seed S] [--vertical V]
    Stage the same rows with the old (v1) and the new (v2) packet into
    DIR/v1 and DIR/v2, and R of them again into DIR/v2_repeat; print the
    judge lines per arm. Runs no judge.
ab-compare DIR [--v1-cutoff N]
    Read both answer sets: rank correlation, top-k overlap, the rows that
    cross the cutoff, and how far v2 moved on the rows judged twice.
"""

from __future__ import annotations

import argparse
import json
import sys

from applyourself import score_store, shortlist
from applyourself.discovery.config import load_config
from applyourself.score_filter import CountryRule, ScoringConfig, lane_cutoffs, load_scoring
from applyourself.shared import clock, paths, verticals
from applyourself.shared.logs import setup_logging


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
            newest_first_seen=load_config().retention_from == "newest",
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
    cut = lane_cutoffs()[lane]
    print(f"== cutoff {cut}: {int((s['fit_score'] >= cut).sum())} at or above")
    top = s.sort_values("fit_score", ascending=False)
    print(top[["fit_score", "suggested_action", "keywords_to_mirror"]].head(15).to_string())
    return 0


def _cmd_examples(args: argparse.Namespace) -> int:
    from applyourself import score_packet

    cfg = load_scoring()
    names = verticals.get_config().names
    if args.vertical and args.vertical not in names:
        print(f"ERROR: unknown vertical {args.vertical!r}")
        return 1
    scored = score_store.read_scored(paths.SCORED)
    for lane in [args.vertical] if args.vertical else names:
        picked = score_packet.choose(
            score_packet.candidates(scored, lane, cfg, paths.STORE, paths.CLEAN)
        )
        text = score_packet.examples_yaml(picked)
        path = score_packet.examples_path(lane)
        print(f"== {lane}: {len(picked)} example(s)")
        print(text, end="")
        if not args.write or not picked:
            continue
        if path.exists() and not args.force:
            print(f"kept {path}: it exists (--force replaces it)")
            continue
        path.write_text(text, encoding="utf-8")
        score_packet.load_examples(lane)
        print(f"wrote {path}")
    return 0


def _cmd_ab_stage(args: argparse.Namespace) -> int:
    from pathlib import Path

    from applyourself import score_ab

    if not paths.CLEAN.exists():
        print("ERROR: jobs/clean.parquet missing — run discovery first")
        return 1
    cfg = load_scoring()
    if args.vertical and args.vertical not in verticals.get_config().names:
        print(f"ERROR: unknown vertical {args.vertical!r}")
        return 1
    res = score_ab.stage(
        Path(args.out),
        cfg,
        rows=args.rows,
        top=args.top,
        repeat=args.repeat,
        seed=args.seed,
        now=clock.now(),
        vertical=args.vertical,
    )
    print(f"rows={res['rows']} out={args.out}")
    for w in res["warnings"]:
        print(f"warning {w}")
    for arm, lines in res["lines"].items():
        print(f"== {arm}: {len(lines)} judge(s)")
        for line in lines:
            print(line)
    return 0


def _cmd_ab_compare(args: argparse.Namespace) -> int:
    from pathlib import Path

    from applyourself import score_ab

    cfg = load_scoring()
    rule = CountryRule.from_discovery(load_config())
    rep = score_ab.compare(Path(args.dir), cfg, rule, args.v1_cutoff)
    for p in rep["problems"]:
        print(f"missing {p}")
    for r in rep["lanes"]:
        print(
            f"{r['lane']}: n={r['n']} rho(v1,v2)={r['rho_v1_v2']} "
            f"rho(stored,v2)={r['rho_stored_v2']} top{r['top_k']} overlap={r['top_overlap']}"
        )
    print(f"crossing the cutoff: {len(rep['crossing'])}")
    for c in rep["crossing"]:
        print(f"- {c['job_id']} {c['lane']} v1={c['v1']:.0f} v2={c['v2']:.0f} {c['role']}")
    c = rep["consistency"]
    print(
        f"v2 judged twice: n={c['n']} fit mean diff={c['fit_mean']} max={c['fit_max']} "
        f"over 3={c['fit_over_3']}; score mean diff={c['score_mean']} max={c['score_max']}; "
        + " ".join(f"same {k}={v}/{c['n']}" for k, v in c["same"].items())
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
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
    p = sub.add_parser("examples", help="pick each lane's calibration examples from past scores")
    p.add_argument("--vertical", metavar="V", help="only this lane")
    p.add_argument(
        "--write", action="store_true", help="save profile/verticals/<lane>/examples.yaml"
    )
    p.add_argument("--force", action="store_true", help="with --write: replace an existing file")
    p.set_defaults(func=_cmd_examples)
    p = sub.add_parser("ab-stage", help="stage the same rows with the v1 and the v2 packet")
    p.add_argument("--out", required=True, metavar="DIR", help="a new, empty folder")
    p.add_argument("--rows", type=int, default=200, help="rows in all (default 200)")
    p.add_argument("--top", type=int, default=50, help="each lane's best-scored rows (default 50)")
    p.add_argument("--repeat", type=int, default=50, help="rows the v2 packet judges twice")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--vertical", metavar="V", help="only this lane")
    p.set_defaults(func=_cmd_ab_stage)
    p = sub.add_parser("ab-compare", help="compare the v1 and v2 answers of an ab-stage folder")
    p.add_argument("dir")
    p.add_argument("--v1-cutoff", type=int, help="the v1 arm's line (default: the lane's cutoff)")
    p.set_defaults(func=_cmd_ab_compare)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
