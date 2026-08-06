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


def _df(title="Widget Assembly Consultant", company="Acme"):
    return pd.DataFrame([{
        "site": "indeed", "company": company, "title": title,
        "job_url": "https://x/1", "description": "d" * 250,
    }])


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


def test_linkedin_uses_linkedin_terms_and_fetches_descriptions(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(jobspy_source, "scrape_jobs",
                        lambda **kw: seen.append(kw) or _df())
    LinkedinSource().fetch(_Ctx(cfg))
    assert {kw["search_term"] for kw in seen} == {
        t for v in cfg.verticals.values() for t in v.linkedin_terms}
    assert all(kw["linkedin_fetch_description"] for kw in seen)
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
