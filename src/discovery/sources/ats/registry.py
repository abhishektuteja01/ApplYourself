from __future__ import annotations

ATS_SOURCE_NAMES = {"greenhouse", "lever", "ashby"}

# Host fragments identifying a URL that leads to the board's own application
# form. Matched against the row's resolved url, not its source: an aggregator
# row whose job_url_direct resolves to a board is applyable too.
ATS_URL_MARKERS = ("greenhouse.io", "lever.co", "ashbyhq.com")
