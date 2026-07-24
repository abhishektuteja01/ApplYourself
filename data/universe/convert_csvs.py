import csv
from pathlib import Path

SRC_DIR = Path("/tmp/ats-scrapers/ats-companies")
DST_DIR = Path(__file__).parent

def clean_csv(ats: str):
    src_file = SRC_DIR / f"{ats}.csv"
    dst_file = DST_DIR / f"{ats}.csv"

    seen = set()
    rows = []

    with open(src_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            slug = (row.get("slug") or "").strip()

            if not slug or not name:
                continue
            if "/" in slug or "http" in slug or "." in slug:
                continue
            if slug in seen:
                continue

            seen.add(slug)
            rows.append({"name": name, "slug": slug, "extra": ""})

    with open(dst_file, "w", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "slug", "extra"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"{ats}: wrote {len(rows)} rows to {dst_file}")

DST_DIR.mkdir(parents=True, exist_ok=True)
for ats in ["greenhouse", "lever", "ashby"]:
    clean_csv(ats)
