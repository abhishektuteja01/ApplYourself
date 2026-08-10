"""Workday: list -> title-classify -> detail (§12b). Unlike the other three
ATS sources, list and detail are separate endpoints and list is paginated, so
this does not reuse `AtsBoardSource` — `fetch_json`/`fetch_json_post` are
patched on `workday` directly rather than on `base`.
"""
from datetime import date

import pytest

from src import verticals as verticals_module
from src.discovery import universe
from src.discovery.universe import UniverseCompany
from src.discovery.sources.ats import workday
from src.discovery.sources.ats.workday import (
    WorkdaySlugError,
    WorkdaySource,
    parse_slug,
    relative_posted_date,
    search_terms,
)


class MockConfigSources:
    pacing_seconds = 0


class MockContext:
    """Mirrors orchestrator.Context, including `.verticals` — the injected
    synthetic fixture (tests/conftest.py's autouse fixture) is the only
    source of Workday search terms in these tests, same as production."""
    class Config:
        sources = {"workday": MockConfigSources}
    config = Config
    deadline_ts = 0.0

    @property
    def verticals(self):
        return verticals_module.get_config()

    def deadline_reached(self):
        return False


LIST_ITEM = {
    "title": "Widget Assembly Consultant",
    "externalPath": "/job/US-CA-Santa-Clara/Widget-Assembly-Consultant_JR1",
    "locationsText": "US, Santa Clara",
    "postedOn": "Posted 3 Days Ago",
    "bulletFields": ["JR1"],
}

DETAIL_PAYLOAD = {
    "jobPostingInfo": {
        "title": "Widget Assembly Consultant",
        "jobDescription": "<p>" + "x" * 250 + "</p>",
        "location": "Santa Clara, California",
        "postedOn": "Posted 3 Days Ago",
        "externalUrl": "https://acme.wd3.myworkdayjobs.com/AcmeExternalCareerSite/job/1",
    }
}


class TestParseSlug:
    def test_the_three_parts(self):
        assert parse_slug("acme|wd3|AcmeExternalCareerSite") == (
            "acme", "wd3", "AcmeExternalCareerSite",
        )

    def test_case_insensitive_pod(self):
        assert parse_slug("acme|WD3|Site")[1] == "WD3"

    @pytest.mark.parametrize("bad", [
        "acme", "acme|wd3", "acme|wd3|site|extra", "acme||site",
        "acme|notwd|site", "|wd3|site",
    ])
    def test_malformed_slugs_raise(self, bad):
        with pytest.raises(WorkdaySlugError):
            parse_slug(bad)


class TestRelativePostedDate:
    def test_today(self):
        assert relative_posted_date("Posted Today", today=date(2026, 7, 10)) == date(2026, 7, 10)

    def test_yesterday(self):
        assert relative_posted_date("Posted Yesterday", today=date(2026, 7, 10)) == date(2026, 7, 9)

    def test_n_days_ago(self):
        assert relative_posted_date("Posted 19 Days Ago", today=date(2026, 7, 10)) == date(2026, 6, 21)

    def test_n_plus_days_ago(self):
        assert relative_posted_date("Posted 30+ Days Ago", today=date(2026, 7, 10)) == date(2026, 6, 10)

    def test_unrecognized_text_is_none(self):
        assert relative_posted_date("Some other phrasing") is None

    def test_missing_is_none(self):
        assert relative_posted_date(None) is None
        assert relative_posted_date("") is None


class TestSearchTerms:
    def test_deduplicates_case_insensitively_across_verticals(self):
        terms = search_terms(verticals_module.get_config())
        assert len(terms) == len({t.strip().casefold() for t in terms})
        assert all(isinstance(t, str) and t for t in terms)

    def test_every_term_comes_from_a_configured_vertical(self):
        cfg = verticals_module.get_config()
        all_configured = {
            t.strip().casefold()
            for v in cfg.verticals.values()
            for t in v.search_terms
        }
        assert set(t.casefold() for t in search_terms(cfg)) <= all_configured


class TestWorkdaySourceFetch:
    def test_one_survivor_becomes_one_row(self, monkeypatch):
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|AcmeExternalCareerSite")],
        )
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM],
        })
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 1
        r = res.rows[0]
        assert r["site"] == "workday"
        assert r["company"] == "Acme AI"
        assert r["title"] == "Widget Assembly Consultant"
        assert r["job_url"] == DETAIL_PAYLOAD["jobPostingInfo"]["externalUrl"]
        assert "xxx" in r["description"]
        assert r["location"] == "Santa Clara, California"
        assert r["date_posted"] is not None
        assert r["vertical"] == "example_primary"

    def test_a_non_classifying_title_never_reaches_detail(self, monkeypatch):
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1,
            "jobPostings": [{**LIST_ITEM, "title": "Definitely Not A Match Zzz"}],
        })

        def boom(*a, **kw):
            raise AssertionError("detail must not be fetched for a non-classifying title")
        monkeypatch.setattr(workday, "fetch_json", boom)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert res.rows == []

    def test_pagination_stops_on_a_short_page_never_on_total(self, monkeypatch):
        """`total` is unreliable past page 0 (module docstring, confirmed
        live) — a full page of length LIST_LIMIT keeps paginating regardless
        of what `total` claims, and only a short page ends it."""
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        calls = []
        full_page = [dict(LIST_ITEM, externalPath=f"/job/{i}") for i in range(workday.LIST_LIMIT)]

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            calls.append((search_text, offset))
            if offset == 0:
                # `total` says there is nothing more, but the page is full —
                # must not be trusted.
                return {"total": 0, "jobPostings": full_page}
            return {"total": 0, "jobPostings": [LIST_ITEM]}

        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        WorkdaySource().fetch(MockContext())
        # Every term's own first page was full, so every term paged at least
        # once past offset 0.
        offsets_by_term = {}
        for term, offset in calls:
            offsets_by_term.setdefault(term, []).append(offset)
        assert offsets_by_term
        for offsets in offsets_by_term.values():
            assert offsets == [0, workday.LIST_LIMIT]

    def test_a_full_page_does_not_loop_forever(self, monkeypatch):
        """Every page returned is exactly LIST_LIMIT long — MAX_PAGES_PER_TERM
        must still cap the crawl for a single term."""
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        full_page = [dict(LIST_ITEM, externalPath=f"/job/{i}") for i in range(workday.LIST_LIMIT)]
        calls = {"n": 0}

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            calls["n"] += 1
            return {"total": 0, "jobPostings": full_page}

        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        WorkdaySource().fetch(MockContext())
        n_terms = len(search_terms(verticals_module.get_config()))
        assert calls["n"] == n_terms * workday.MAX_PAGES_PER_TERM

    def test_the_same_posting_under_two_terms_is_detail_fetched_once(self, monkeypatch):
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM],
        })
        detail_calls = []

        def fake_detail(url, **kw):
            detail_calls.append(url)
            return DETAIL_PAYLOAD
        monkeypatch.setattr(workday, "fetch_json", fake_detail)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 1
        assert len(detail_calls) == 1

    def test_a_malformed_slug_is_a_per_company_error_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Broken Co", "workday", "not-a-triple"),
            UniverseCompany("Acme AI", "workday", "acme|wd3|Site"),
        ])
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM],
        })
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 1
        assert any("Broken Co" in e for e in res.errors)

    def test_a_detail_fetch_failure_does_not_lose_other_survivors(self, monkeypatch):
        from src.discovery.sources.ats.http import CareersError

        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        second_item = {**LIST_ITEM, "title": "Widget Fabrication Consultant",
                       "externalPath": "/job/2"}
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM, second_item],
        })

        def fake_detail(url, **kw):
            if url.endswith("/job/2"):
                raise CareersError("gone", status=404, permanent=True)
            return DETAIL_PAYLOAD
        monkeypatch.setattr(workday, "fetch_json", fake_detail)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 1
        assert res.errors

    def test_every_detail_fetch_failing_the_same_way_is_raised_not_swallowed(self, monkeypatch):
        """A detail response every survivor's fetch cannot parse points at a
        Workday schema change breaking the parser, not two dead postings —
        raising it stops the run from reporting a healthy, empty shard."""
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("A Co", "workday", "a|wd1|Site"),
            UniverseCompany("B Co", "workday", "b|wd1|Site"),
        ])
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM],
        })
        # No "jobPostingInfo" key -- _detail_row raises TypeError on every call.
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: {})
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        with pytest.raises(TypeError):
            WorkdaySource().fetch(MockContext())

    def test_list_endpoint_failure_is_a_per_company_error(self, monkeypatch):
        from src.discovery.sources.ats.http import CareersError

        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Broken Co", "workday", "badco|wd3|Site"),
            UniverseCompany("Acme AI", "workday", "acme|wd3|Site"),
        ])

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            if company == "badco":
                raise CareersError("board not found (404)", status=404, permanent=True)
            return {"total": 1, "jobPostings": [LIST_ITEM]}
        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 1
        assert any("Broken Co" in e for e in res.errors)


class TestListPage:
    def test_posts_the_expected_body_and_url(self, monkeypatch):
        seen = {}

        def fake_post(url, body, **kw):
            seen["url"] = url
            seen["body"] = body
            return {"total": 0, "jobPostings": []}
        monkeypatch.setattr(workday, "fetch_json_post", fake_post)

        workday.list_page("acme", "wd3", "AcmeExternalCareerSite", 0)
        assert seen["url"] == (
            "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeExternalCareerSite/jobs"
        )
        assert seen["body"] == {
            "appliedFacets": {}, "limit": workday.LIST_LIMIT, "offset": 0, "searchText": "",
        }

    def test_non_dict_payload_raises(self, monkeypatch):
        monkeypatch.setattr(workday, "fetch_json_post", lambda *a, **kw: [])
        with pytest.raises(TypeError):
            workday.list_page("acme", "wd3", "Site", 0)

    def test_missing_job_postings_key_raises(self, monkeypatch):
        monkeypatch.setattr(workday, "fetch_json_post", lambda *a, **kw: {"total": 0})
        with pytest.raises(TypeError):
            workday.list_page("acme", "wd3", "Site", 0)
