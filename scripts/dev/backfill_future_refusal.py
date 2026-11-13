"""Add `future_refusal` to judged rows' judge_facts in scored.parquet (one-time).

`score finish` stores the JD's sentence refusing sponsorship now or in the
future (`score_filter.future_refusal`) for every new row; `derive` reads it.
This fills it for rows judged before, from the posting that was judged (the
one whose trimmed JD gives the row's stored judge_input_hash under today's
lane packet or one with an earlier shared rubric, from git history; else the
first) and the role's clean.parquet row, the posting a new judge would read:
the first refusal of the two.
Only judge_facts changes: never the judge's answer, the hashes or
scored_at, so nothing turns stale. A row with no JD left gets no key.
--dry-run prints the counts and every row whose label or score would
change; --apply writes, under the store's lock.

  uv run python scripts/dev/backfill_future_refusal.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from applyourself import score_store  # noqa: E402
from applyourself.score_filter import (  # noqa: E402
    CountryRule,
    future_refusal,
    load_scoring,
    trim_jd,
)
from applyourself.shared import paths, schemas, verticals  # noqa: E402
from applyourself.shared.parquet_io import locked, schema_version, write_parquet  # noqa: E402

Posting = tuple[str, str]  # (title, jd_text)


def rubric_versions() -> list[str]:
    """Every committed text of the shared scoring rubric, newest first; []
    outside a git checkout."""
    rel = str(score_store.SHARED_RUBRIC.relative_to(REPO))
    try:
        shas = subprocess.run(
            ["git", "-C", str(REPO), "log", "--format=%H", "--", rel],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return [
            subprocess.run(
                ["git", "-C", str(REPO), "show", f"{sha}:{rel}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            for sha in shas
        ]
    except (OSError, subprocess.CalledProcessError):
        return []


def hashed_packets(cfg: Any, rubrics: list[str]) -> dict[str, list[str]]:
    """lane -> its packet as input_hash reads it, today's first, then one per
    older shared rubric."""
    lanes = verticals.get_config().verticals
    out = {n: [score_store.packet_text(lane, cfg, "", for_hash=True)] for n, lane in lanes.items()}
    today = score_store.SHARED_RUBRIC
    with tempfile.TemporaryDirectory() as tmp:
        for i, text in enumerate(rubrics):
            path = Path(tmp) / f"rubric_{i}.md"
            path.write_text(text, encoding="utf-8")
            score_store.SHARED_RUBRIC = path
            try:
                for n, lane in lanes.items():
                    out[n].append(score_store.packet_text(lane, cfg, "", for_hash=True))
            finally:
                score_store.SHARED_RUBRIC = today
    return out


def postings(clean: Path, store: Path, ids: set[str]) -> tuple[dict[str, list[Posting]], set[str]]:
    """(job_id -> its postings: the clean.parquet row first, then the
    store's, newest first; the ids clean.parquet holds)."""
    out: dict[str, list[Posting]] = {}
    live: set[str] = set()
    for path in (clean, store):
        if not path.exists():
            continue
        cols = ["job_id", "title", "jd_text"] + (["last_seen"] if path == store else [])
        df = pd.read_parquet(path, columns=cols)
        df = df[df["job_id"].isin(ids)]
        if "last_seen" in df:
            df = df.sort_values("last_seen", ascending=False)
        for j, t, jd in zip(df["job_id"], df["title"], df["jd_text"], strict=True):
            if isinstance(jd, str) and jd:
                out.setdefault(str(j), []).append((str(t or ""), jd))
        if path == clean:
            live = set(out)
    return out, live


def judged_posting(
    found: list[Posting], stored_hash: Any, packets: list[str], cfg: Any
) -> Posting | None:
    """The posting whose trimmed JD gives `stored_hash` under one of
    `packets`, else the first posting; None with none."""
    if isinstance(stored_hash, str) and stored_hash:
        for title, jd in found:
            trimmed = trim_jd(jd, cfg)
            if any(score_store.input_hash(p, trimmed) == stored_hash for p in packets):
                return title, jd
    return found[0] if found else None


def plan(
    scored: pd.DataFrame,
    found: dict[str, list[Posting]],
    live: set[str],
    packets: dict[str, list[str]],
    cfg: Any,
    rule: CountryRule,
) -> tuple[list[str | None], list[tuple[str, ...]]]:
    """(the new judge_facts per row, None where unchanged; the rows whose
    label or score changes). `found[id][0]` is the clean.parquet row for an
    id in `live`."""
    lanes = verticals.get_config().verticals
    out: list[str | None] = []
    changed: list[tuple[str, ...]] = []
    for r in scored.to_dict("records"):
        answer = json.loads(r["judge_answer"]) if r["judge_answer"] else None
        facts = json.loads(r["judge_facts"]) if r["judge_facts"] else None
        jid = str(r["job_id"])
        posting = judged_posting(
            found.get(jid, []), r["judge_input_hash"], packets.get(r["vertical"], []), cfg
        )
        if (
            not score_store.is_v2(answer)
            or facts is None
            or "future_refusal" in facts
            or posting is None
        ):
            out.append(None)
            continue
        texts = [posting] + ([found[jid][0]] if jid in live else [])
        refusal = next((x for t, jd in texts if (x := future_refusal(f"{t}\n{jd}"))), "")
        new = {**facts, "future_refusal": refusal}
        out.append(json.dumps(new))
        lane = lanes.get(r["vertical"])
        if lane is None or not new["future_refusal"]:
            continue
        before = score_store.derive(answer, lane, facts, cfg, rule, "", r["scored_at"])
        after = score_store.derive(answer, lane, new, cfg, rule, "", r["scored_at"])
        if (before["sponsorship_label"], before["fit_score"]) != (
            after["sponsorship_label"],
            after["fit_score"],
        ):
            changed.append(
                (
                    jid,
                    str(r["vertical"]),
                    f"{before['sponsorship_label']} -> {after['sponsorship_label']}",
                    f"{before['fit_score']:.0f} -> {after['fit_score']:.0f}",
                    new["future_refusal"][:160],
                )
            )
    return out, changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--scored", type=Path, default=paths.SCORED)
    ap.add_argument("--clean", type=Path, default=paths.CLEAN)
    ap.add_argument("--store", type=Path, default=paths.STORE)
    args = ap.parse_args(argv)
    if not args.scored.exists():
        print(f"{args.scored} not found: nothing to backfill")
        return 0
    from applyourself.discovery.config import load_config

    cfg = load_scoring()
    rule = CountryRule.from_discovery(load_config())
    packets = hashed_packets(cfg, rubric_versions())
    with locked(args.scored):
        schemas.check(schema_version(args.scored), schemas.SCORED_SCHEMA_VERSION, args.scored)
        df = pd.read_parquet(args.scored)
        scored = score_store.read_scored(args.scored)
        found, live = postings(args.clean, args.store, set(map(str, scored["job_id"])))
        new, changed = plan(scored, found, live, packets, cfg, rule)
        filled = [v for v in new if v is not None]
        hits = sum(1 for v in filled if json.loads(v)["future_refusal"])
        print(f"{len(filled)} judged rows to fill of {len(df)}; {hits} with a refusal sentence")
        print(f"{len(changed)} rows change label or score:")
        for row in changed:
            print("  " + " | ".join(row))
        if args.apply and filled:
            by_id = dict(zip(scored["job_id"], new, strict=True))
            df["judge_facts"] = [
                by_id.get(j) or old for j, old in zip(df["job_id"], df["judge_facts"], strict=True)
            ]
            write_parquet(df, args.scored, schema_version=schemas.SCORED_SCHEMA_VERSION)
    print("written" if args.apply and filled else "nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
