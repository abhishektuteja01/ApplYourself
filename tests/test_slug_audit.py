"""scripts/audit_slugs.py: the four payload summarizers and the status call.

The script's value is entirely in telling `empty` and `stale` apart from
`active` -- the two states the nightly run cannot see. Everything else it does
is paced HTTP and a CSV writer. So the tests are on the pure parts: what each
board family's payload says about count and newest-posting date, and what
`classify` concludes from the pair.
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "audit_slugs.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_slugs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit():
    return _load()


TODAY = date(2026, 9, 21)


class TestClassify:
    def test_no_postings_is_empty(self, audit):
        assert audit.classify(0, None, 90, TODAY) == audit.EMPTY

    def test_recent_posting_is_active(self, audit):
        assert audit.classify(12, TODAY - timedelta(days=3), 90, TODAY) == audit.ACTIVE

    def test_posting_older_than_the_window_is_stale(self, audit):
        assert audit.classify(12, TODAY - timedelta(days=400), 90, TODAY) == audit.STALE

    def test_the_boundary_day_is_not_yet_stale(self, audit):
        assert audit.classify(1, TODAY - timedelta(days=90), 90, TODAY) == audit.ACTIVE
        assert audit.classify(1, TODAY - timedelta(days=91), 90, TODAY) == audit.STALE

    def test_unparseable_dates_read_active_not_stale(self, audit):
        """Unknown age is not evidence of abandonment; calling it stale would
        recommend dropping live boards."""
        assert audit.classify(30, None, 90, TODAY) == audit.ACTIVE


class TestGreenhouse:
    def test_counts_and_takes_the_newest_date(self, audit):
        payload = {"jobs": [
            {"updated_at": "2026-01-02T00:00:00Z"},
            {"first_published": "2026-08-09T10:00:00Z", "updated_at": "2026-09-01T00:00:00Z"},
        ]}
        assert audit.greenhouse_summary(payload) == (2, date(2026, 8, 9))

    def test_empty_board_is_zero_not_an_error(self, audit):
        assert audit.greenhouse_summary({"jobs": []}) == (0, None)

    def test_wrong_shape_raises(self, audit):
        with pytest.raises(TypeError):
            audit.greenhouse_summary({"jobs": "nope"})
        with pytest.raises(TypeError):
            audit.greenhouse_summary([])


class TestAshby:
    def test_reads_published_at(self, audit):
        payload = {"jobs": [
            {"publishedAt": "2026-03-04T00:00:00Z"},
            {"publishedAt": "2026-07-08T00:00:00+00:00"},
        ]}
        assert audit.ashby_summary(payload) == (2, date(2026, 7, 8))

    def test_missing_dates_still_count_the_postings(self, audit):
        assert audit.ashby_summary({"jobs": [{}, {}]}) == (2, None)


class TestLever:
    def test_reads_epoch_millis_from_a_bare_array(self, audit):
        ms = int(date(2026, 6, 1).strftime("%s")) * 1000
        assert audit.lever_summary([{"createdAt": ms}]) == (1, date(2026, 6, 1))

    def test_a_dict_payload_is_a_malformed_board(self, audit):
        with pytest.raises(TypeError):
            audit.lever_summary({"jobs": []})


class TestWorkday:
    def test_prefers_page_zero_total_over_the_page_length(self, audit):
        payload = {"total": 240, "jobPostings": [{"postedOn": "Posted Today"}]}
        count, newest = audit.workday_summary(payload)
        assert count == 240
        assert newest == date.today()

    def test_a_total_below_the_page_length_is_ignored(self, audit):
        """`total` is documented as untrustworthy; one smaller than the page in
        hand is provably wrong, so the postings win."""
        payload = {"total": 0, "jobPostings": [{"postedOn": "Posted 3 Days Ago"}, {}]}
        count, _ = audit.workday_summary(payload)
        assert count == 2

    def test_relative_text_it_cannot_parse_yields_no_date(self, audit):
        """"30+ Days Ago" does parse (as exactly 30); an undated board does not,
        and a count with no date lands on `active` by classify's rule."""
        payload = {"total": 5, "jobPostings": [{"postedOn": "Posted recently"}]}
        assert audit.workday_summary(payload) == (5, None)


class TestBoardUrls:
    def test_the_audit_fetches_lighter_payloads_than_the_scrapers(self, audit):
        """A liveness call needs titles and dates, not every full JD."""
        assert "content=true" not in audit.board_url("greenhouse", "acme")
        assert "includeCompensation" not in audit.board_url("ashby", "acme")

    def test_every_summarizer_has_a_lane_and_vice_versa(self, audit):
        from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES

        assert set(audit.SUMMARIZE) == set(ATS_SOURCE_NAMES)
        assert set(audit.DEFAULT_ATS) <= set(audit.SUMMARIZE)

    def test_an_unknown_ats_raises(self, audit):
        with pytest.raises(ValueError):
            audit.board_url("workday", "acme")
