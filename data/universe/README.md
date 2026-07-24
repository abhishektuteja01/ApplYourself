# ATS Universe Data

This directory contains tenant lists (company slugs) for various ATS boards, used by the discovery pipeline to scrape boards without needing explicit manual entry in the watchlist.

## Provenance
Seed lists cloned from [github.com/kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers).

## License
MIT License.

## Refresh Procedure
1. Clone the repository: `git clone https://github.com/kalil0321/ats-scrapers.git /tmp/ats-scrapers`
2. Run `data/universe/convert_csvs.py` to extract and deduplicate `greenhouse`, `lever`, and `ashby` slugs.
3. Commit the updated CSV files.
