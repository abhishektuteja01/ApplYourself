# ATS Universe Data

This directory contains tenant lists (company slugs) for various ATS boards, used by the discovery pipeline to scrape boards without needing explicit manual entry in the watchlist.

## Provenance
Seed lists cloned from [github.com/kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers).

## License
MIT License. Copyright (c) kalil0321 (github.com/kalil0321/ats-scrapers).

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, subject to the condition that the above
copyright notice and this permission notice be included in all copies or
substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT
WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.

Slugs added since the seed clone were verified individually against the live
board API before being committed.

The Workday rows have no ats-scrapers seed at all: Workday's tri-part slug
(`company|wd#|site_id`) is not a shape that source publishes, so every one was
hand-verified against the live CXS API before being committed.

## The board list

`boards.csv` is the one board list, `ats,name,slug`, one row per (ats, slug)
whatever the slug's case, with `ats` one of greenhouse, lever, ashby, workday,
eightfold. It also carries bulk slug lists added after the seed clone.
`boards_retired.csv` (same columns) holds the spellings retired from it.
`sources/boards/base.py:load_boards` reads both:

- A spelling in `boards_retired.csv` is retired, and stays retired if it is
  re-added to `boards.csv`. When one spelling is retired and another is live,
  the live one is used: Lever's API is case-sensitive (`fliff` 404s where
  `Fliff` serves). An old-layout `active=0` row in `boards.csv` still retires.
- `name` comes from `boards.csv` and must be spelled the way the board spells
  it: it feeds `job_id`.

`jobs/boards.parquet` records, per board, when it was last checked, when it last
had a job that passed the title gate, and last check's counts. Three 404s in a
row mark a board `dead`, re-checked every `dead_every_days`. A 404 on its own
never justifies retiring it: it conflates a never-real slug, a renamed board, a
platform move and a casing artifact. `scripts/audit_slugs.py` reports which is
which; moving a row to `boards_retired.csv` is a hand edit.

## Refresh procedure

```bash
git clone https://github.com/kalil0321/ats-scrapers.git /tmp/ats-scrapers
uv run python - <<'PY'
import csv, pathlib
out = pathlib.Path("data/universe/boards.csv")
rows = {(r["ats"], r["slug"].lower()): r for r in csv.DictReader(out.open(encoding="utf-8"))}
gone = out.with_name("boards_retired.csv")
retired = {(r["ats"], r["slug"].lower()) for r in csv.DictReader(gone.open(encoding="utf-8"))}
for ats in ("greenhouse", "lever", "ashby"):
    for p in sorted(pathlib.Path("/tmp/ats-scrapers/ats-companies").glob(f"*{ats}*")):
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                slug = (r.get("slug") or r.get("token") or "").strip()
                name = (r.get("name") or r.get("company") or "").strip()
                if slug and name and (ats, slug.lower()) not in retired:
                    rows.setdefault((ats, slug.lower()), {"ats": ats, "name": name, "slug": slug})
with out.open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["ats", "name", "slug"], lineterminator="\n")
    w.writeheader()
    w.writerows(sorted(rows.values(), key=lambda r: (r["ats"], r["slug"].lower())))
PY
```

Then commit `boards.csv`.
