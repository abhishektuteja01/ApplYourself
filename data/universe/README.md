# ATS Universe Data

This directory contains tenant lists (company slugs) for various ATS boards, used by the discovery pipeline to scrape boards without needing explicit manual entry in the watchlist.

## Provenance
Seed lists cloned from [github.com/kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers).

## License
MIT License.

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

## Refresh Procedure
Each `<ats>.csv` here is `name,slug` with a header, deduplicated on `slug`.

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
