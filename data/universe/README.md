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

`workday.csv` has no ats-scrapers seed at all (§12b) — Workday's tri-part
slug (`company|wd#|site_id`) is not a shape that source publishes, so every
row was hand-verified against the live CXS API, one tenant at a time,
before being committed.

## `<ats>.local.csv` — untracked overlay

`universe.load()` reads `<ats>.local.csv` before `<ats>.csv` and merges both on
`slug`, so a local file extends the tracked list. Tracked loads second and wins
the name on any overlapping slug; the `profile/companies.yaml` watchlist still
wins over both.

These files are gitignored and optional — nothing here depends on one existing.
They are the place for bulk slug dumps whose license does not permit
redistribution under this repo's MIT license. Only self-verified slugs go in the
tracked CSV.

Expect a slower first few runs after a bulk addition: dead slugs are pruned only
after 3 consecutive 404s.

## `active` — the audit's tombstone column

Each `<ats>.csv` is `name,slug[,active]`. `active` is `0` or `1`; a missing
column, a blank cell and an unparseable value all read as live, so every file
written before the column existed keeps working untouched.

`active=0` is written **only** by the audit — `scripts/audit_slugs.py` and its
reconcile pass — and read only by `universe._load_csv`. Discovery never writes
it. It is a different claim from the health ledger's `pruned_at`:

| | writer | claim | reversible |
|---|---|---|---|
| `active=0` | the audit | this slug is a dead end, or the company moved and the live slug is listed separately | by hand only |
| `pruned_at` | the nightly run | healthy board, three consecutive 404s, resting 14 days | automatically |

Two properties the column exists for:

- **A tombstone outlives a re-add.** Retirements are unioned across `<ats>.csv`
  and `<ats>.local.csv` and applied after both have loaded, so the next bulk
  dump into the overlay cannot resurrect a slug the audit already disproved.
- **The watchlist still wins.** `profile/companies.yaml` loads after the
  retirements, so naming a slug by hand overrides the verdict.

A retired slug leaves `universe_slugs()` too, so its health row is pruned as an
orphan. `universe_companies(ats, include_retired=True)` is the only way back in,
and only an audit re-checking its own verdicts may pass it —
`audit_slugs.py --include-retired` does exactly that.

A 404 on its own never justifies `active=0`. It means only that the API has no
board under that exact string, which conflates a never-real seed slug, a
renamed board, a platform move, and a Lever casing artifact (Lever's API is
case-sensitive: `fliff` 404s where `Fliff` serves). Only a verdict of `moved`
(on the old row, once the new slug is confirmed live) or `gone` (nothing found
by any tier, and no candidate domain resolves) may set it.

## Refresh Procedure
Each `<ats>.csv` here is `name,slug[,active]` with a header, deduplicated on `slug`.

```bash
git clone https://github.com/kalil0321/ats-scrapers.git /tmp/ats-scrapers
for ats in greenhouse lever ashby; do
  uv run python - "$ats" <<'PY' > "data/universe/$ats.csv"
import csv, sys, pathlib
ats = sys.argv[1]
rows, seen = [], set()
for p in sorted(pathlib.Path("/tmp/ats-scrapers/ats-companies").glob(f"*{ats}*")):
    with p.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("slug") or r.get("token") or "").strip()
            name = (r.get("name") or r.get("company") or "").strip()
            if slug and name and slug not in seen:
                seen.add(slug)
                rows.append((name, slug))
w = csv.writer(sys.stdout)
w.writerow(["name", "slug"])
w.writerows(sorted(rows, key=lambda r: r[1]))
PY
done
```

Then commit the updated CSV files.
