# ATS Universe Data

This directory contains tenant lists (company slugs) for various ATS boards, used by the discovery pipeline to scrape boards without needing explicit manual entry in the watchlist.

## Provenance
Seed lists cloned from [github.com/kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers).

## License
MIT License.

## Refresh Procedure
Each `<ats>.csv` here is `name,slug` with a header, deduplicated on `slug`.

```bash
git clone https://github.com/kalil0321/ats-scrapers.git /tmp/ats-scrapers
for ats in greenhouse lever ashby; do
  python3 - "$ats" <<'PY' > "data/universe/$ats.csv"
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
