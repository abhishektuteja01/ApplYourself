"""`config` — read-only views of profile/ config for command prose and scripts.

  config get settings KEY      one value from profile/settings.yaml
  config get discovery KEY     one resolved field of profile/discovery.yaml
  config verticals [LANE]      lanes and default; with LANE, its term and rule counts
  config classify TITLE ...    the lane each title goes to, and why the gate drops it
  config misclassified LANE    the lane's search terms a board title would put elsewhere
  config reclassify            clean.parquet rows whose lane or gate verdict would change now
  config work-auth [COUNTRY]   the answer bank's work-authorization row
  config migrate --dry-run|--apply [--profile DIR]
                               move an older profile/ layout onto the current one

Nothing here writes, except `migrate --apply`.
"""

from __future__ import annotations

import argparse
import sys

from applyourself.shared import paths
from applyourself.shared.logs import setup_logging


def _get(section: str, key: str) -> object:
    if section == "settings":
        from applyourself.shared.config_schema import load_settings

        data = load_settings(paths.SETTINGS)
        if key not in data:
            raise ValueError(f"settings.yaml has no `{key}`")
        return data[key]
    from applyourself.discovery.config import load_config

    cfg = load_config()
    if not hasattr(cfg, key):
        raise ValueError(f"discovery.yaml has no `{key}`")
    return getattr(cfg, key)


def _verticals(lane: str | None) -> list[str]:
    from applyourself.shared.verticals import load_verticals

    c = load_verticals()
    out = [f"lanes: {' '.join(c.names)}", f"default: {c.default_vertical}"]
    if lane is None:
        return out
    if lane not in c.verticals:
        raise ValueError(f"no lane `{lane}`")
    v = c.verticals[lane]
    rules = [i for i, (vt, _) in enumerate(c.classifier_rules) if vt == lane]
    return out + [
        f"search: {len(v.search_terms)} linkedin: {len(v.linkedin_terms)}",
        f"title gate: ban {len(v.title_exclude_terms)} include {len(v.title_include_terms)}"
        f" strong_keep {len(v.title_strong_keep_terms)}",
        f"years: {v.years}",
        f"rules for this lane: {rules} of {len(c.classifier_rules)}",
    ]


def _misclassified(lane: str) -> str:
    from applyourself.shared.title_gate import lane_for
    from applyourself.shared.verticals import get_config

    v = get_config().verticals[lane]
    terms = dict.fromkeys(v.search_terms + v.linkedin_terms + v.abroad_terms)
    bad = [t for t in terms if lane_for(t, "", "greenhouse").lane != lane]
    return f"MISCLASSIFIED: {bad or 'none'}"


def _reclassify() -> list[str]:
    import pandas as pd

    from applyourself.shared import schemas
    from applyourself.shared.parquet_io import read_parquet
    from applyourself.shared.title_gate import lane_for

    d = read_parquet(paths.CLEAN, schemas.CLEAN_SCHEMA_VERSION)
    got = [
        lane_for(t, v if isinstance(v, str) else "", s)
        for t, v, s in zip(d.title, d.vertical, d.source, strict=True)
    ]
    now = pd.Series([g.lane if g.passes else f"({g.reason})" for g in got], index=d.index)
    moved = d[now != d.vertical.fillna("")]
    return [
        f"{len(moved)} rows would reclassify",
        moved.assign(new=now[moved.index])[["title", "vertical", "new"]].head(30).to_string(),
    ]


def _work_auth(country: str) -> str:
    from applyourself.apply.bank_schema import (
        AnswersError,
        load_answers,
    )

    try:
        a = load_answers()
    except AnswersError as exc:
        raise ValueError(str(exc)) from exc
    country = country or a.home_country
    f = a.facts_for(country)
    return f"{country} {f.authorized} {f.requires_sponsorship} {f.status_label!r} {f.followup!r}"


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(
        prog="config", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("section", choices=("settings", "discovery"))
    g.add_argument("key")
    v = sub.add_parser("verticals")
    v.add_argument("lane", nargs="?")
    cl = sub.add_parser("classify")
    cl.add_argument("titles", nargs="+")
    m = sub.add_parser("misclassified")
    m.add_argument("lane")
    sub.add_parser("reclassify")
    w = sub.add_parser("work-auth")
    w.add_argument("country", nargs="?", default="")
    mg = sub.add_parser("migrate")
    mode = mg.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mg.add_argument("--profile", help="the profile/ folder (default: this repo's)")
    args = ap.parse_args(argv)
    if args.cmd == "migrate":
        from applyourself import config_migrate

        return config_migrate.main(args)
    try:
        if args.cmd == "get":
            print(_get(args.section, args.key))
        elif args.cmd == "verticals":
            print("\n".join(_verticals(args.lane)))
        elif args.cmd == "classify":
            from applyourself.shared.title_gate import lane_for

            for t in args.titles:
                got = lane_for(t)
                print(
                    f"{t} -> {got.lane or '(none)'}"
                    + (f" ({got.reason})" if got.lane and got.reason else "")
                )
        elif args.cmd == "misclassified":
            print(_misclassified(args.lane))
        elif args.cmd == "reclassify":
            print("\n".join(_reclassify()))
        else:
            print(_work_auth(args.country))
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
