from __future__ import annotations

ATS_SOURCE_NAMES = {"greenhouse", "lever", "ashby", "workday"}

# Host fragments identifying a URL that leads to the board's own application
# form. Matched against the row's resolved url, not its source: an aggregator
# row whose job_url_direct resolves to a board is applyable too.
#
# "myworkdayjobs.com" belongs here even though Workday is manual-apply
# (§12b): this marker decides which URL survives dedupe, not whether /apply
# can submit to it — a human still needs the real application URL, not an
# aggregator repost, to apply by hand. Submission dispatch is a separate,
# independent list (`apply_cli.py`'s `_ATS_PARSERS`), which has no Workday
# entry and never will.
ATS_URL_MARKERS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com")
