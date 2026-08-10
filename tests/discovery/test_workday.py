"""Workday: list -> title-classify -> detail (§12b). Unlike the other three
ATS sources, list and detail are separate endpoints and list is paginated, so
this does not reuse `AtsBoardSource` — `fetch_json`/`fetch_json_post` are
patched on `workday` directly rather than on `base`.
"""
from datetime import date

import pytest

from src.discovery import universe
from src.discovery.universe import UniverseCompany
from src.discovery.sources.ats import workday
from src.discovery.sources.ats.workday import (
    WorkdaySlugError,
    WorkdaySource,
    parse_slug,
    relative_posted_date,
)


class MockConfigSources:
    pacing_seconds = 0


class MockContext:
    class Config:
        sources = {"workday": MockConfigSources}
    config = Config
    deadline_ts = 0.0

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

    def test_pagination_stops_once_offset_reaches_total(self, monkeypatch):
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        calls = []

        def fake_list_page(company, wd, site_id, offset, deadline_ts=None):
            calls.append(offset)
            if offset == 0:
                return {"total": 2 * workday.LIST_LIMIT, "jobPostings": [LIST_ITEM]}
            return {"total": 2 * workday.LIST_LIMIT, "jobPostings": []}

        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        WorkdaySource().fetch(MockContext())
        # An empty second page ends the loop even though total says there is
        # room for a third — a page with nothing on it will never fill.
        assert calls == [0, workday.LIST_LIMIT]

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

    def test_list_endpoint_failure_is_a_per_company_error(self, monkeypatch):
        from src.discovery.sources.ats.http import CareersError

        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Broken Co", "workday", "badco|wd3|Site"),
            UniverseCompany("Acme AI", "workday", "acme|wd3|Site"),
        ])

        def fake_list_page(company, wd, site_id, offset, deadline_ts=None):
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
