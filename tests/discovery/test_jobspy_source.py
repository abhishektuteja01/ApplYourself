"""JobSpySource.fetch — term iteration, vertical stamping, error isolation.

scrape_jobs is patched everywhere: jobspy is an external scraper and the suite
makes no network calls. The `enabled` flag is a runtime gate read only by the
orchestrator, so it does not affect these.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.discovery.sources import jobspy_source
from src.discovery.sources.jobspy_source import IndeedSource, LinkedinSource


class _Ctx:
    """Minimal orchestrator context: what fetch() actually reads."""

    def __init__(self, cfg, countries=("United States",), deadline_after=None):
        class Src:
            pacing_seconds = 0.0
        self.config = type("Config", (), {
            "sources": {"linkedin": Src, "indeed": Src},
            "location_allowlist": type("L", (), {"countries": list(countries)}),
        })
        self.verticals = cfg
        self._deadline_after = deadline_after
        self.calls = 0

    def deadline_reached(self):
        if self._deadline_after is None:
            return False
        reached = self.calls >= self._deadline_after
        self.calls += 1
        return reached


POSTED_DATE = "2026-08-01"


def _df(title="Widget Assembly Consultant", company="Acme",
        job_url="https://www.linkedin.com/jobs/view/1"):
    return pd.DataFrame([{
        "site": "indeed", "company": company, "title": title,
        "job_url": job_url, "description": "d" * 250,
        "date_posted": POSTED_DATE,
    }])


# A title carrying every vertical's include term and none of their excludes, so
# it passes the gate whichever vertical's search term returned it.
ANY_VERTICAL = "Widget Sprocket Cog Analyst"
# Matches no vertical's include terms — fails the gate everywhere.
NO_VERTICAL = "Unrelated Manager"


class _FakeDetails:
    """Stands in for the jobspy LinkedIn client, recording every job id asked
    for. `empty_for` ids return {} the way the real bare-except path does."""

    def __init__(self, empty_for=()):
        self.asked = []
        self._empty_for = set(empty_for)

    def _get_job_details(self, job_id):
        self.asked.append(job_id)
        if job_id in self._empty_for:
            return {}
        return {
            "description": f"jd for {job_id}",
            "job_level": "Mid-Senior level",
            "job_type": None,
            "job_url_direct": f"https://acme.example/{job_id}",
        }


def _fake_details(monkeypatch, empty_for=()):
    client = _FakeDetails(empty_for)
    monkeypatch.setattr(jobspy_source, "_linkedin_detail_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(jobspy_source.time, "sleep", lambda _: None)


def test_queries_every_term_location_and_remote_flag(cfg, monkeypatch):
    seen = []

    def fake_scrape(**kw):
        seen.append((kw["search_term"], kw["location"], kw["is_remote"]))
        return _df()

    monkeypatch.setattr(jobspy_source, "scrape_jobs", fake_scrape)
    IndeedSource().fetch(_Ctx(cfg, countries=("United States", "Canada")))

    terms = {t for t, _, _ in seen}
    expected_terms = {t for v in cfg.verticals.values() for t in v.search_terms}
    assert terms == expected_terms
    # Every term is queried against both locations and both remote flags.
    assert len(seen) == len(expected_terms) * 2 * 2
    assert {r for _, _, r in seen} == {False, True}


def test_linkedin_uses_linkedin_terms_and_defers_descriptions(cfg, monkeypatch):
    """The inline fetch is off on every query: descriptions are backfilled
    after the term loop, once per unique gate-passing url."""
    seen = []
    monkeypatch.setattr(jobspy_source, "scrape_jobs",
                        lambda **kw: seen.append(kw) or _df())
    _fake_details(monkeypatch)
    LinkedinSource().fetch(_Ctx(cfg))
    assert {kw["search_term"] for kw in seen} == {
        t for v in cfg.verticals.values() for t in v.linkedin_terms}
    assert not any(kw["linkedin_fetch_description"] for kw in seen)
    assert all(kw["site_name"] == "linkedin" for kw in seen)


def test_rows_are_stamped_with_the_vertical_that_found_them(cfg, monkeypatch):
    """The stamp is the authoritative signal; cleaning's title classifier is
    only a fallback for rows that arrive without one."""
    term_to_vertical = {t: v.name for v in cfg.verticals.values()
                        for t in v.search_terms}
    monkeypatch.setattr(jobspy_source, "scrape_jobs",
                        lambda **kw: _df(company=kw["search_term"]))
    res = IndeedSource().fetch(_Ctx(cfg))
    assert res.rows
    for row in res.rows:
        assert row["vertical"] == term_to_vertical[row["company"]]


def test_one_failing_term_does_not_lose_the_others(cfg, monkeypatch):
    all_terms = sorted({t for v in cfg.verticals.values() for t in v.search_terms})
    doomed = all_terms[0]

    def fake_scrape(**kw):
        if kw["search_term"] == doomed:
            raise RuntimeError("429 rate limited")
        return _df()

    monkeypatch.setattr(jobspy_source, "scrape_jobs", fake_scrape)
    res = IndeedSource().fetch(_Ctx(cfg))
    assert len(res.errors) == 2          # one location x both remote flags
    assert all(doomed in e and "429 rate limited" in e for e in res.errors)
    assert res.rows                       # the surviving terms still produced rows


def test_a_none_dataframe_is_not_an_error(cfg, monkeypatch):
    monkeypatch.setattr(jobspy_source, "scrape_jobs", lambda **kw: None)
    res = IndeedSource().fetch(_Ctx(cfg))
    assert res.errors == []
    assert res.rows == []


def test_deadline_short_circuits_and_keeps_the_rows_gathered_so_far(cfg, monkeypatch):
    monkeypatch.setattr(jobspy_source, "scrape_jobs", lambda **kw: _df())
    ctx = _Ctx(cfg, deadline_after=3)
    res = IndeedSource().fetch(ctx)
    # Stopped early, but nothing already scraped is thrown away.
    assert len(res.rows) == 3
    assert f"Queries made: {len(res.rows)}" not in res.report_lines


class TestDeferredDescriptions:
    """LinkedIn descriptions: one fetch per unique gate-passing url, after the
    term loop, backfilled onto every row sharing that url."""

    def test_one_url_across_many_rows_is_fetched_once_and_backfills_all(
            self, cfg, monkeypatch):
        monkeypatch.setattr(jobspy_source, "scrape_jobs",
                            lambda **kw: _df(title=ANY_VERTICAL))
        client = _fake_details(monkeypatch)
        res = LinkedinSource().fetch(_Ctx(cfg))

        assert len(res.rows) > 1              # every query returned the same url
        assert client.asked == ["1"]          # fetched exactly once
        assert all(r["description"] == "jd for 1" for r in res.rows)
        assert all(r["job_url_direct"] == "https://acme.example/1" for r in res.rows)

    def test_a_url_found_under_two_verticals_keeps_both_rows(self, cfg, monkeypatch):
        """Dedupe applies to the fetch set only. Dropping rows stays cleaning's
        job, so the shard is row-for-row what an inline fetch would produce."""
        monkeypatch.setattr(jobspy_source, "scrape_jobs",
                            lambda **kw: _df(title=ANY_VERTICAL))
        client = _fake_details(monkeypatch)
        res = LinkedinSource().fetch(_Ctx(cfg))

        verticals_seen = {r["vertical"] for r in res.rows}
        assert len(verticals_seen) > 1
        assert len(client.asked) == 1

    def test_gate_failures_are_never_fetched(self, cfg, monkeypatch):
        def fake_scrape(**kw):
            passes = _df(title=ANY_VERTICAL,
                         job_url="https://www.linkedin.com/jobs/view/11")
            fails = _df(title=NO_VERTICAL,
                        job_url="https://www.linkedin.com/jobs/view/22")
            return pd.concat([passes, fails], ignore_index=True)

        monkeypatch.setattr(jobspy_source, "scrape_jobs", fake_scrape)
        client = _fake_details(monkeypatch)
        res = LinkedinSource().fetch(_Ctx(cfg))

        assert client.asked == ["11"]
        # The gate failure is still written to the shard: jobs/raw stays a
        # complete archive, and cleaning drops it on title before the
        # short-JD check ever sees the empty description.
        failed = [r for r in res.rows if r["title"] == NO_VERTICAL]
        assert failed
        assert all(r["description"] == "d" * 250 for r in failed)

    def test_indeed_never_fetches_details(self, cfg, monkeypatch):
        monkeypatch.setattr(jobspy_source, "scrape_jobs",
                            lambda **kw: _df(title=ANY_VERTICAL))
        client = _fake_details(monkeypatch)
        IndeedSource().fetch(_Ctx(cfg))
        assert client.asked == []

    def test_an_empty_detail_page_is_counted_not_silent(self, cfg, monkeypatch):
        """_get_job_details swallows request errors and returns {}. The count
        has to reach the run report or a broken jobspy reads as row loss."""
        monkeypatch.setattr(jobspy_source, "scrape_jobs",
                            lambda **kw: _df(title=ANY_VERTICAL))
        _fake_details(monkeypatch, empty_for={"1"})
        res = LinkedinSource().fetch(_Ctx(cfg))

        assert any("1 attempted (0 filled, 1 empty)" in line
                   for line in res.report_lines)
        # Nothing was overwritten with a blank.
        assert all(r["description"] == "d" * 250 for r in res.rows)

    def test_backfill_touches_only_the_detail_fields(self, cfg, monkeypatch):
        """Load-bearing for the 14-day window and for job_id.

        `posted_date` feeds drop_stale's cutoff and company/title feed the
        job_id hash — all three come from the search card, and the detail page
        must never be able to move them. _detail_values whitelists, so a
        payload carrying them writes nothing."""
        monkeypatch.setattr(jobspy_source, "scrape_jobs",
                            lambda **kw: _df(title=ANY_VERTICAL))

        class _Overreaching(_FakeDetails):
            def _get_job_details(self, job_id):
                out = super()._get_job_details(job_id)
                out.update({"date_posted": "1970-01-01", "company": "Wrong",
                            "title": "Wrong", "job_url": "https://wrong"})
                return out

        client = _Overreaching()
        monkeypatch.setattr(jobspy_source, "_linkedin_detail_client", lambda: client)

        res = LinkedinSource().fetch(_Ctx(cfg))

        assert res.rows
        for row in res.rows:
            assert row["description"] == "jd for 1"      # backfill did happen
            assert row["date_posted"] == POSTED_DATE     # the drop_stale input
            assert row["company"] == "Acme"              # both job_id inputs
            assert row["title"] == ANY_VERTICAL
            assert row["job_url"] == "https://www.linkedin.com/jobs/view/1"

    def test_deadline_during_the_detail_loop_keeps_the_rows(self, cfg, monkeypatch):
        """The term loop finished, so the search-card fields are real work.
        Stop fetching, keep the rows, and say so in the report."""
        urls = iter(range(1, 500))
        monkeypatch.setattr(
            jobspy_source, "scrape_jobs",
            lambda **kw: _df(title=ANY_VERTICAL,
                             job_url=f"https://www.linkedin.com/jobs/view/{next(urls)}"))
        client = _fake_details(monkeypatch)

        ctx = _Ctx(cfg)
        n_queries = len({t for v in cfg.verticals.values() for t in v.linkedin_terms}) * 2
        ctx._deadline_after = n_queries + 2   # trips two urls into the detail loop
        res = LinkedinSource().fetch(ctx)

        assert len(client.asked) == 2
        assert len(res.rows) == n_queries      # every scraped row survives
        assert any("DEADLINE REACHED" in line for line in res.report_lines)


class TestLinkedinJobId:
    def test_canonical_url(self):
        assert jobspy_source._linkedin_job_id(
            "https://www.linkedin.com/jobs/view/4420402826") == "4420402826"

    def test_slug_url(self):
        """A raw search href carries a title slug ahead of the id — splitting
        on '-' alone would return the whole canonical url instead."""
        assert jobspy_source._linkedin_job_id(
            "https://www.linkedin.com/jobs/view/staff-engineer-at-acme-4420402826"
        ) == "4420402826"

    def test_query_string_and_trailing_slash(self):
        assert jobspy_source._linkedin_job_id(
            "https://www.linkedin.com/jobs/view/123/?refId=x") == "123"

    @pytest.mark.parametrize("url", ["", None, "https://example.com/careers/abc"])
    def test_unparseable(self, url):
        assert jobspy_source._linkedin_job_id(url) is None


def test_jobspy_still_exposes_the_private_detail_api():
    """We call an underscore-prefixed jobspy method. python-jobspy is pinned
    <1.2; this fails in CI on an upgrade that moves it, rather than at 02:00
    in the middle of a run."""
    from jobspy.linkedin import LinkedIn
    import inspect

    assert callable(LinkedIn._get_job_details)
    assert list(inspect.signature(LinkedIn._get_job_details).parameters) == [
        "self", "job_id"]
