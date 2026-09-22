"""Workday: list -> title-classify -> detail (§12b). Unlike the other three
ATS sources, list and detail are separate endpoints and list is paginated, so
this does not reuse `AtsBoardSource` — `fetch_json`/`fetch_json_post` are
patched on `workday` directly rather than on `base`.
"""
import pandas as pd
import pytest

from src import verticals as verticals_module
from src.discovery import universe
from src.discovery.universe import UniverseCompany
from src.discovery.sources.ats import workday
from src.discovery.sources.ats.workday import (
    WorkdaySlugError,
    WorkdaySource,
    parse_slug,
    search_terms,
)


class MockConfigSources:
    pacing_seconds = 0
    # Every tenant every run: these tests are about the crawl, not the rotation.
    cold_rotation_runs = 1
    pacing_spread = 3.0
    detail_reuse_days = 0


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


def _hot(name="Acme AI", slug="acme|wd3|Site"):
    """A watchlist tenant: hot every run, so it reads deep pages."""
    return UniverseCompany(name, "workday", slug, priority=True)


def _unique_full_page(search_text, offset):
    """A full page whose postings no other page or term repeats, so the
    no-new-match early stop never fires."""
    return [dict(LIST_ITEM, externalPath=f"/job/{search_text}-{offset}-{i}")
            for i in range(workday.LIST_LIMIT)]


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


class TestSearchTerms:
    def test_one_term_per_vertical_not_the_union(self):
        """Tenant x term is the cost. One term per lane makes the other lanes
        reachable at all; the union would multiply the crawl by the term list."""
        cfg = verticals_module.get_config()
        terms = search_terms(cfg)
        assert len(terms) == len(cfg.verticals)
        assert set(terms) == {v.search_terms[0] for v in cfg.verticals.values()}

    def test_the_default_vertical_leads(self):
        """A deadline cut has to starve the other lanes before the main one."""
        cfg = verticals_module.get_config()
        assert search_terms(cfg)[0] == cfg.verticals[cfg.default_vertical].search_terms[0]

    def test_a_term_shared_by_two_lanes_is_crawled_once(self):
        from dataclasses import replace

        cfg = verticals_module.get_config()
        shared = cfg.verticals[cfg.default_vertical].search_terms[0]
        collided = replace(cfg, verticals={
            name: (v if name == cfg.default_vertical
                   else replace(v, search_terms=[shared] + list(v.search_terms)))
            for name, v in cfg.verticals.items()
        })
        assert search_terms(collided) == (shared,)

    def test_a_lane_with_no_search_terms_is_skipped_not_fatal(self):
        from dataclasses import replace

        cfg = verticals_module.get_config()
        other = next(n for n in cfg.verticals if n != cfg.default_vertical)
        thinned = replace(cfg, verticals={
            name: (replace(v, search_terms=[]) if name == other else v)
            for name, v in cfg.verticals.items()
        })
        assert len(search_terms(thinned)) == len(cfg.verticals) - 1


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
        monkeypatch.setattr(universe, "load", lambda ats: [_hot()])
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
        monkeypatch.setattr(universe, "load", lambda ats: [_hot()])
        calls = {"n": 0}

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            calls["n"] += 1
            return {"total": 0, "jobPostings": _unique_full_page(search_text, offset)}

        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        WorkdaySource().fetch(MockContext())
        n_terms = len(search_terms(verticals_module.get_config()))
        assert calls["n"] == n_terms * workday.MAX_PAGES_PER_TERM

    def test_a_gate_failing_title_never_reaches_detail(self, monkeypatch):
        """`classify_vertical_from_title` is the cheap filter; cleaning's
        `apply_title_exclusion` is the strict one, and running it only at
        cleaning time paid for a detail fetch per row it was going to drop."""
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        # Classifies example_primary on "widget", then trips the "senior"
        # title_exclude_term.
        excluded = {**LIST_ITEM, "title": "Senior Widget Consultant",
                    "externalPath": "/job/2"}
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 2, "jobPostings": [LIST_ITEM, excluded],
        })
        detail_calls = []

        def fake_detail(url, **kw):
            detail_calls.append(url)
            return DETAIL_PAYLOAD
        monkeypatch.setattr(workday, "fetch_json", fake_detail)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(detail_calls) == 1
        assert not any(url.endswith("/job/2") for url in detail_calls)
        assert len(res.rows) == 1

    def test_health_records_the_kept_count_not_the_fetched_count(self, monkeypatch):
        """`universe.load` sorts by `last_yield`, so it has to mean "postings
        relevant to this profile", not "postings on the board"."""
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        off_lane = {**LIST_ITEM, "title": "Definitely Not A Match Zzz",
                    "externalPath": "/job/2"}
        excluded = {**LIST_ITEM, "title": "Senior Widget Consultant",
                    "externalPath": "/job/3"}
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 3, "jobPostings": [LIST_ITEM, off_lane, excluded],
        })
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        recorded = []
        monkeypatch.setattr(
            universe.HealthLedger, "mark_ok",
            lambda self, slug, kept=0: recorded.append((True, kept)))

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 1
        assert recorded == [(True, 1)]

    def test_the_same_posting_is_detail_fetched_once(self, monkeypatch):
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

    def test_rows_record_the_term_that_found_them(self, monkeypatch):
        """The term is known in the list loop and out of scope by the row
        build — `survivors` has to carry it across."""
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany("Acme AI", "workday", "acme|wd3|Site")],
        )
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM],
        })
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        term = search_terms(verticals_module.get_config())[0]
        assert [r["found_by_term"] for r in res.rows] == [term]
        # Workday never queries a remote-only variant.
        assert all(r["found_by_remote"] is False for r in res.rows)

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
        from src.ats_http import CareersError

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
        Workday schema change breaking the parser, not a few dead postings —
        raising it stops the run from reporting a healthy, empty shard.

        Needs `ESCALATE_MIN_ATTEMPTS` tenants: the check is a failure *rate*
        over a minimum sample, not "every attempt so far". The old form was
        order-dependent — two bad postings at the head of a run aborted the
        whole lane, and a 98% failure rate later in the same run was silent.
        """
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany(f"Co {i}", "workday", f"c{i}|wd1|Site")
            for i in range(workday.ESCALATE_MIN_ATTEMPTS + 2)
        ])
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM],
        })
        # No "jobPostingInfo" key -- _detail_row raises TypeError on every call.
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: {})
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        with pytest.raises(RuntimeError, match="unparseable"):
            WorkdaySource().fetch(MockContext())

    def test_a_couple_of_dead_postings_at_the_head_do_not_abort_the_lane(
        self, monkeypatch
    ):
        """The failure this replaces: two malformed payloads at the *start* of
        a run used to raise, because errors == attempts was trivially true.
        Two dead postings on the first tenant is an ordinary event."""
        calls = {"n": 0}

        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany(f"Co {i}", "workday", f"c{i}|wd1|Site") for i in range(6)
        ])
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM],
        })

        def flaky(url, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                return {}                       # unparseable: no jobPostingInfo
            return {"jobPostingInfo": dict(DETAIL_PAYLOAD["jobPostingInfo"])}

        monkeypatch.setattr(workday, "fetch_json", flaky)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert res.rows, "the healthy tenants after the two bad ones must still yield"
        assert any("malformed detail" in e for e in res.errors)

    def test_the_escalation_ratio_counts_company_term_pairs(self, monkeypatch):
        """`shape_errors` is counted per (company, term). Counting attempts per
        company put the ratio above 1.0 as soon as a second term existed — one
        consistently malformed term would abort the whole lane."""
        monkeypatch.setattr(workday, "search_terms", lambda cfg: ("term-a", "term-b"))
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany(f"Co {i}", "workday", f"c{i}|wd1|Site") for i in range(12)
        ])

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            if search_text == "term-b":
                raise TypeError("expected a list under 'jobPostings', got NoneType")
            return {"total": 1, "jobPostings": [LIST_ITEM]}

        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 12
        assert len(res.errors) == 12

    def test_a_couple_of_malformed_list_payloads_do_not_escalate(self, monkeypatch):
        monkeypatch.setattr(workday, "search_terms", lambda cfg: ("term-a", "term-b"))
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany(f"Co {i}", "workday", f"c{i}|wd1|Site") for i in range(12)
        ])

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            if search_text == "term-b" and company in ("c0", "c1"):
                raise TypeError("expected a list under 'jobPostings', got NoneType")
            return {"total": 1, "jobPostings": [LIST_ITEM]}

        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        res = WorkdaySource().fetch(MockContext())
        assert len(res.rows) == 12
        assert len(res.errors) == 2

    def test_list_endpoint_failure_is_a_per_company_error(self, monkeypatch):
        from src.ats_http import CareersError

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


class TestRelaxedSleep:
    def test_sleeps_at_least_pacing_and_varies_across_calls(self, monkeypatch):
        # Deterministic pacing but random duration — a fixed interval is the
        # bot tell this exists to avoid, so two calls at the same pacing must
        # not sleep the identical amount.
        slept: list[float] = []
        monkeypatch.setattr(workday.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(workday.random, "random", lambda: 1.0)  # skip the rare long pause

        for _ in range(20):
            workday._relaxed_sleep(2.0)

        assert all(s >= 2.0 for s in slept)
        assert len(set(slept)) > 1

    def test_never_sleeps_below_pacing(self, monkeypatch):
        monkeypatch.setattr(workday.random, "uniform", lambda lo, hi: lo)
        monkeypatch.setattr(workday.random, "random", lambda: 1.0)
        slept: list[float] = []
        monkeypatch.setattr(workday.time, "sleep", lambda s: slept.append(s))

        workday._relaxed_sleep(3.5)

        assert slept == [3.5]


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

    def test_list_limit_does_not_exceed_workdays_confirmed_ceiling(self):
        # Live-confirmed against 3M, Accenture, Adobe and Cisco's real
        # Search/jobs endpoint: limit=20 returns 200 on every tenant tried,
        # limit=21 returns 400 on every tenant tried, no variance. This is
        # a hard server-side cap, not a tenant quirk — a future bump of
        # LIST_LIMIT above 20 reintroduces the 2026-08-10 outage (2640/2640
        # list POSTs failing with HTTP 400, the very first call for every
        # tenant).
        assert workday.LIST_LIMIT <= 20


class TestTheCrawlResumesAcrossRuns:
    """55 tenants x 48 terms is 2,640 list requests before a posting is read.
    A run cannot cover that, so it covers a slice and the next one continues.
    """

    def _companies(self, n=6, hot=False):
        return [UniverseCompany(f"Co {i}", "workday", f"c{i}|wd1|Site", priority=hot)
                for i in range(n)]

    def _stub(self, monkeypatch, companies, seen):
        monkeypatch.setattr(universe, "load", lambda ats: companies)

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            seen.append((company, search_text, offset))
            return {"total": 0, "jobPostings": []}

        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

    def test_a_deadline_cut_does_not_starve_the_same_tail_every_run(self, monkeypatch):
        """Tenant order is (priority, last_yield) with no shuffle, so a run
        that stops early used to drop the identical alphabetical tail forever
        — those tenants were never crawled on any run."""
        companies = self._companies()

        class StopsAfterTwo(MockContext):
            """`deadline_reached()` is checked several times per tenant (once
            per company, per term, and per page), not once — so the cutoff
            has to be a fixed budget generous enough for a couple of tenants
            to complete, not `len(search_terms(...))`-scaled: with a single
            configured term that shrank to 2, too tight to let even one
            tenant's first page be read before the cut."""

            def __init__(self):
                self.polled = 0

            def deadline_reached(self):
                self.polled += 1
                return self.polled > 12

        first: list = []
        self._stub(monkeypatch, companies, first)
        WorkdaySource().fetch(StopsAfterTwo())
        first_tenants = list(dict.fromkeys(c for c, _, _ in first))

        second: list = []
        self._stub(monkeypatch, companies, second)
        WorkdaySource().fetch(StopsAfterTwo())
        second_tenants = list(dict.fromkeys(c for c, _, _ in second))

        assert first_tenants, "the first run must crawl something"
        assert second_tenants, "the second run must crawl something"
        assert second_tenants[0] != first_tenants[0], (
            "the second run restarted at the same tenant — the tail still starves"
        )

    def test_paging_reads_page_zero_every_run_then_resumes_the_deep_frontier(
        self, monkeypatch
    ):
        """Both halves matter, and an earlier version got the trade backwards.

        MAX_PAGES_PER_TERM caps one pair's paging, so without a persisted
        offset pages past the cap were never read on ANY run. But resuming
        *at* the saved offset skips page 0 — and Workday's list endpoint takes
        no sort parameter, so its default ordering puts the newest postings
        first. A busy pair would then go several runs blind to exactly the new
        postings discovery exists to find.

        So: page 0 every run, then continue from the frontier.
        """
        companies = self._companies(1, hot=True)
        calls: list[int] = []

        def always_full(company, wd, site_id, offset, search_text="", deadline_ts=None):
            calls.append(offset)
            return {"total": 0, "jobPostings": _unique_full_page(search_text, offset)}

        monkeypatch.setattr(universe, "load", lambda ats: companies)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)
        monkeypatch.setattr(workday, "list_page", always_full)

        WorkdaySource().fetch(MockContext())
        first = list(calls)
        calls.clear()
        WorkdaySource().fetch(MockContext())
        second = list(calls)

        cap = workday.MAX_PAGES_PER_TERM * workday.LIST_LIMIT
        per_term = workday.MAX_PAGES_PER_TERM
        assert first[:per_term] == [i * workday.LIST_LIMIT for i in range(per_term)]
        # Freshness: the head is re-read, not skipped.
        assert second[0] == 0
        # Depth: the run still advances past where the last one stopped.
        assert max(second[:per_term]) > max(first[:per_term])
        assert second[1] == cap

    def test_a_deadline_cut_after_page_zero_leaves_the_frontier_where_it_was(
        self, monkeypatch
    ):
        """Page 0 is the freshness read, not the frontier read.

        Letting a full page 0 write `0 + LIST_LIMIT` walked the frontier
        *backwards*: a pair sitting at 200 that read the head and then hit the
        deadline came back as 50, losing the depth earlier runs had bought.
        Invisible on a fresh pair, where the frontier defaults to LIST_LIMIT
        and the two values coincide — so it bit only the deep pairs the cursor
        exists for.
        """
        from src.discovery import crawl_cursor as cc

        companies = self._companies(1, hot=True)
        slug = companies[0].slug
        terms = search_terms(verticals_module.get_config())
        full = [dict(LIST_ITEM, externalPath=f"/job/{i}")
                for i in range(workday.LIST_LIMIT)]

        cursor = cc.CrawlCursor(ats="workday")
        for term in terms:
            cursor.set_offset(slug, term, 200)
        cc.save_cursor(cursor)

        reads = {"n": 0}

        def one_full_page(company, wd, site_id, offset, search_text="",
                          deadline_ts=None):
            reads["n"] += 1
            return {"total": 0, "jobPostings": full}

        ctx = MockContext()
        # Cut the run the moment any page has been read, so every term gets
        # its page 0 and nothing deeper.
        ctx.deadline_reached = lambda: reads["n"] > 0

        monkeypatch.setattr(universe, "load", lambda ats: companies)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)
        monkeypatch.setattr(workday, "list_page", one_full_page)

        WorkdaySource().fetch(ctx)

        after = cc.load_cursor("workday")
        assert after.offset_for(slug, terms[0]) == 200, (
            "a full page 0 followed by a deadline cut must not rewrite the "
            "frontier"
        )

    def test_an_exhausted_pair_starts_over_next_run(self, monkeypatch):
        """A short page means the term is exhausted — the next run should
        re-crawl it from the top to pick up newly posted roles, not keep
        paging into empty space."""
        seen: list = []
        self._stub(monkeypatch, self._companies(1), seen)
        WorkdaySource().fetch(MockContext())
        seen.clear()
        WorkdaySource().fetch(MockContext())
        assert seen and all(offset == 0 for _, _, offset in seen)

    def test_a_corrupt_cursor_file_does_not_break_the_crawl(self, monkeypatch):
        from src.discovery import crawl_cursor as cc
        cc.CURSOR_DIR.mkdir(parents=True, exist_ok=True)
        cc.cursor_path("workday").write_text("{not json", encoding="utf-8")

        seen: list = []
        self._stub(monkeypatch, self._companies(2), seen)
        WorkdaySource().fetch(MockContext())
        assert seen, "a bad cursor must mean 'start from the top', not a crash"


class TestTheFrontierIsCapped:
    """The list endpoint never reports exhaustion past page 0, so an uncapped
    frontier walks forever — two tenants reached offsets 3,220 and 3,420,
    pages 161-176 of a single query."""

    def _company(self):
        return [_hot(slug="acme|wd1|Site")]

    def _stub_full_pages(self, monkeypatch, calls):
        def always_full(company, wd, site_id, offset, search_text="", deadline_ts=None):
            calls.append(offset)
            return {"total": 0, "jobPostings": _unique_full_page(search_text, offset)}

        monkeypatch.setattr(universe, "load", lambda ats: self._company())
        monkeypatch.setattr(workday, "list_page", always_full)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

    def _seed(self, slug, offset):
        from src.discovery import crawl_cursor as cc
        cursor = cc.CrawlCursor(ats="workday")
        for term in search_terms(verticals_module.get_config()):
            cursor.set_offset(slug, term, offset)
        cc.save_cursor(cursor)
        return cc

    def test_a_run_that_would_cross_the_cap_restarts_at_the_head(self, monkeypatch):
        slug = self._company()[0].slug
        cc = self._seed(slug, workday.MAX_FRONTIER_OFFSET - workday.LIST_LIMIT)
        calls: list[int] = []
        self._stub_full_pages(monkeypatch, calls)

        WorkdaySource().fetch(MockContext())

        assert max(calls) > workday.MAX_FRONTIER_OFFSET - workday.LIST_LIMIT
        term = search_terms(verticals_module.get_config())[0]
        assert cc.load_cursor("workday").offset_for(slug, term) == 0

    def test_a_pair_already_past_the_cap_wraps_instead_of_walking_on(self, monkeypatch):
        slug = self._company()[0].slug
        self._seed(slug, 3220)
        calls: list[int] = []
        self._stub_full_pages(monkeypatch, calls)

        WorkdaySource().fetch(MockContext())

        assert calls[0] == 0
        assert max(calls) <= workday.MAX_PAGES_PER_TERM * workday.LIST_LIMIT
        assert 3220 not in calls

    def test_the_short_page_reset_still_wins(self, monkeypatch):
        """An exhausted pair restarts at the head whatever the cap says."""
        slug = self._company()[0].slug
        cc = self._seed(slug, 100)
        monkeypatch.setattr(universe, "load", lambda ats: self._company())
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 0, "jobPostings": [LIST_ITEM]})
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        WorkdaySource().fetch(MockContext())

        term = search_terms(verticals_module.get_config())[0]
        assert cc.load_cursor("workday").offset_for(slug, term) == 0


class TestListYieldCounters:
    """Three buckets, not two: whether deep pages contribute anything has to be
    answerable from the report."""

    def test_new_repeat_and_unclassified_paths_are_counted_separately(self, monkeypatch):
        matched = [dict(LIST_ITEM, externalPath=f"/job/m{i}") for i in range(10)]
        unmatched = [dict(LIST_ITEM, title="Definitely Not A Match Zzz",
                          externalPath=f"/job/u{i}") for i in range(10)]
        page = matched + unmatched

        def fake_list(company, wd, site_id, offset, search_text="", deadline_ts=None):
            if offset in (0, workday.LIST_LIMIT):
                return {"total": 0, "jobPostings": page}
            return {"total": 0, "jobPostings": []}

        monkeypatch.setattr(universe, "load", lambda ats: [_hot(slug="acme|wd1|Site")])
        monkeypatch.setattr(workday, "list_page", fake_list)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        lines = WorkdaySource().fetch(MockContext()).report_lines
        # Two pages x 20 items x one term per vertical. Only the first term
        # sees a path as new; every later term re-sees the same 40 as repeats.
        terms = len(verticals_module.get_config().verticals)
        assert (f"List paths: 10 new+classified | 10 new, no vertical "
                f"| {40 * terms - 20} repeat") in lines


class TestPerTenantReportTable:
    """D2.9: the table was only ever appended for `c.priority` tenants, and
    `companies.yaml` has no workday entries — so it never rendered."""

    def _tenants(self, monkeypatch, counts: dict[str, int]):
        monkeypatch.setattr(
            universe, "load",
            lambda ats: [UniverseCompany(name, "workday", f"{name.lower()}|wd3|Site")
                         for name in counts],
        )

        def fake_list(company, wd, site_id, offset, term, **kw):
            if offset:
                return {"total": 0, "jobPostings": []}
            n = counts[company.capitalize()] if company.capitalize() in counts \
                else counts[company]
            return {"total": n, "jobPostings": [
                {**LIST_ITEM, "externalPath": f"/job/{company}/{i}"}
                for i in range(n)]}

        monkeypatch.setattr(workday, "list_page", fake_list)
        monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: DETAIL_PAYLOAD)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

    def test_table_renders_for_non_priority_tenants(self, monkeypatch):
        self._tenants(monkeypatch, {"Acme": 1, "Beta": 3, "Gamma": 2})
        lines = WorkdaySource().fetch(MockContext()).report_lines
        assert "| company | status | fetched | kept | error |" in lines
        rows = [ln for ln in lines if ln.startswith("| ") and " OK " in ln]
        assert [ln.split(" | ")[0].lstrip("| ") for ln in rows] == \
            ["Beta", "Gamma", "Acme"]

    def test_only_the_top_n_tenants_are_listed(self, monkeypatch):
        monkeypatch.setattr(workday, "REPORT_TOP_TENANTS", 2)
        self._tenants(monkeypatch, {"Acme": 1, "Beta": 3, "Gamma": 2})
        lines = WorkdaySource().fetch(MockContext()).report_lines
        rows = [ln for ln in lines if ln.startswith("| ") and " OK " in ln]
        assert [ln.split(" | ")[0].lstrip("| ") for ln in rows] == ["Beta", "Gamma"]


class TestHotColdSelection:
    """Workday takes the board lanes' split: every hot tenant each run, then
    one rotating slice of the cold tail."""

    def _ctx(self, rotation_runs):
        class Src:
            pacing_seconds = 0
            cold_rotation_runs = rotation_runs
            pacing_spread = 3.0
            detail_reuse_days = 0

        class Ctx(MockContext):
            class Config:
                sources = {"workday": Src}
            config = Config
        return Ctx()

    def _run(self, monkeypatch, companies, ctx):
        polled: list[str] = []

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            polled.append(company)
            return {"total": 0, "jobPostings": []}
        monkeypatch.setattr(universe, "load", lambda ats: list(companies))
        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)
        res = WorkdaySource().fetch(ctx)
        return set(polled), res

    def test_hot_tenants_every_run_and_one_cold_slice(self, monkeypatch):
        today = pd.Timestamp.today().normalize()
        hot = [UniverseCompany("Hot %d" % i, "workday", "hot%d|wd1|s" % i,
                               last_kept_at=today) for i in range(2)]
        cold = [UniverseCompany("Cold %d" % i, "workday", "cold%d|wd1|s" % i)
                for i in range(6)]
        ctx = self._ctx(3)

        seen_cold: list[set] = []
        for _ in range(3):
            polled, res = self._run(monkeypatch, hot + cold, ctx)
            assert {"hot0", "hot1"} <= polled
            seen_cold.append(polled - {"hot0", "hot1"})
        assert all(len(s) == 2 for s in seen_cold)
        # Three runs at 1/3 cover the whole cold tail exactly once.
        assert set().union(*seen_cold) == {"cold%d" % i for i in range(6)}
        assert res.report_lines[0].startswith("Companies polled: 4 of 8 (2 hot, 2 of 6 cold)")

    def test_a_deadline_cut_resumes_the_cold_rotation_where_it_stopped(self, monkeypatch):
        cold = [UniverseCompany("Cold %d" % i, "workday", "cold%d|wd1|s" % i)
                for i in range(4)]
        ctx = self._ctx(1)
        order: list[str] = []

        def fake_list_page(company, wd, site_id, offset, search_text="", deadline_ts=None):
            order.append(company)
            return {"total": 0, "jobPostings": []}
        monkeypatch.setattr(universe, "load", lambda ats: list(cold))
        monkeypatch.setattr(workday, "list_page", fake_list_page)
        monkeypatch.setattr(workday.time, "sleep", lambda _: None)

        # The deadline lands after the first tenant's first request.
        ctx.deadline_reached = lambda: bool(order)
        WorkdaySource().fetch(ctx)
        assert order == ["cold0"]

        order.clear()
        ctx.deadline_reached = lambda: False
        WorkdaySource().fetch(ctx)
        assert order[0] == "cold1"


class TestRelaxedSleep:
    def test_sleep_stays_between_pacing_and_pacing_times_spread(self, monkeypatch):
        slept = []
        monkeypatch.setattr(workday.time, "sleep", slept.append)
        monkeypatch.setattr(workday.random, "random", lambda: 1.0)  # no long pause
        for _ in range(200):
            got = workday._relaxed_sleep(1.0, 2.0)
            assert 1.0 <= got <= 2.0
        assert all(1.0 <= s <= 2.0 for s in slept)

    def test_the_long_pause_is_added_and_returned(self, monkeypatch):
        slept = []
        monkeypatch.setattr(workday.time, "sleep", slept.append)
        monkeypatch.setattr(workday.random, "random", lambda: 0.0)
        got = workday._relaxed_sleep(1.0, 2.0)
        assert len(slept) == 2 and got == sum(slept)
        assert 5.0 <= slept[1] <= 15.0


def _stub_details(monkeypatch):
    calls = []

    def fake_detail(url, **kw):
        calls.append(url)
        return DETAIL_PAYLOAD
    monkeypatch.setattr(workday, "fetch_json", fake_detail)
    monkeypatch.setattr(workday.time, "sleep", lambda _: None)
    return calls


class TestColdTenantsReadTheHeadOnly:
    def test_a_cold_tenant_reads_page_zero_only_and_keeps_its_frontier(self, monkeypatch):
        from src.discovery import crawl_cursor as cc

        cold = UniverseCompany("Acme AI", "workday", "acme|wd3|Site")
        terms = search_terms(verticals_module.get_config())
        cursor = cc.CrawlCursor(ats="workday")
        for term in terms:
            cursor.set_offset(cold.slug, term, 200)
        cc.save_cursor(cursor)

        offsets = []

        def always_full(company, wd, site_id, offset, search_text="", deadline_ts=None):
            offsets.append(offset)
            return {"total": 0, "jobPostings": _unique_full_page(search_text, offset)}

        monkeypatch.setattr(universe, "load", lambda ats: [cold])
        monkeypatch.setattr(workday, "list_page", always_full)
        _stub_details(monkeypatch)

        res = WorkdaySource().fetch(MockContext())
        assert offsets == [0] * len(terms)
        assert cc.load_cursor("workday").offset_for(cold.slug, terms[0]) == 200
        assert any(f"{len(terms)} cold head-only" in line for line in res.report_lines)

    def test_a_hot_tenant_still_reads_deep(self, monkeypatch):
        offsets = []

        def always_full(company, wd, site_id, offset, search_text="", deadline_ts=None):
            offsets.append(offset)
            return {"total": 0, "jobPostings": _unique_full_page(search_text, offset)}

        monkeypatch.setattr(universe, "load", lambda ats: [_hot()])
        monkeypatch.setattr(workday, "list_page", always_full)
        _stub_details(monkeypatch)

        WorkdaySource().fetch(MockContext())
        assert any(o > 0 for o in offsets)


class TestEarlyStop:
    def test_a_full_deep_page_with_no_new_match_ends_the_term(self, monkeypatch):
        """Page 20 repeats page 0, so it brings nothing new: stop there, and
        still move the frontier past it."""
        from src.discovery import crawl_cursor as cc

        monkeypatch.setattr(workday, "search_terms", lambda cfg: ("term-a",))
        page = [dict(LIST_ITEM, externalPath=f"/job/{i}") for i in range(workday.LIST_LIMIT)]
        offsets = []

        def repeating(company, wd, site_id, offset, search_text="", deadline_ts=None):
            offsets.append(offset)
            return {"total": 0, "jobPostings": page}

        monkeypatch.setattr(universe, "load", lambda ats: [_hot()])
        monkeypatch.setattr(workday, "list_page", repeating)
        _stub_details(monkeypatch)

        res = WorkdaySource().fetch(MockContext())
        assert offsets == [0, workday.LIST_LIMIT]
        assert cc.load_cursor("workday").offset_for("acme|wd3|Site", "term-a") == \
            2 * workday.LIST_LIMIT
        assert any("1 early stops" in line for line in res.report_lines)

    def test_a_deep_page_of_only_unclassified_titles_also_stops(self, monkeypatch):
        monkeypatch.setattr(workday, "search_terms", lambda cfg: ("term-a",))
        offsets = []

        def fake(company, wd, site_id, offset, search_text="", deadline_ts=None):
            offsets.append(offset)
            if offset == 0:
                return {"total": 0, "jobPostings": _unique_full_page("a", 0)}
            return {"total": 0, "jobPostings": [
                dict(LIST_ITEM, title="Definitely Not A Match Zzz",
                     externalPath=f"/job/u{offset}-{i}")
                for i in range(workday.LIST_LIMIT)]}

        monkeypatch.setattr(universe, "load", lambda ats: [_hot()])
        monkeypatch.setattr(workday, "list_page", fake)
        _stub_details(monkeypatch)

        WorkdaySource().fetch(MockContext())
        assert offsets == [0, workday.LIST_LIMIT]


class TestDescriptionReuse:
    HOST_URL = "https://acme.wd3.myworkdayjobs.com/Site"

    def _saved_shard(self, raw_dir, run_id, path=LIST_ITEM["externalPath"],
                     description="saved description " + "y" * 200):
        raw_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{
            "job_url": self.HOST_URL + path, "title": "Widget Assembly Consultant",
            "location": "Saved City", "description": description,
        }]).to_parquet(raw_dir / f"{run_id}_workday.parquet")

    def _ctx(self, days):
        class Src(MockConfigSources):
            detail_reuse_days = days

        class Ctx(MockContext):
            class Config:
                sources = {"workday": Src}
            config = Config
        return Ctx()

    def _run(self, monkeypatch, tmp_path, days):
        monkeypatch.setattr(workday, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(workday, "search_terms", lambda cfg: ("term-a",))
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Acme AI", "workday", "acme|wd3|Site")])
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM]})
        calls = _stub_details(monkeypatch)
        return WorkdaySource().fetch(self._ctx(days)), calls

    def _today_run_id(self, days_ago=0):
        return (pd.Timestamp.today() - pd.Timedelta(days=days_ago)).strftime("%Y-%m-%d_2000")

    def test_a_saved_description_skips_the_detail_fetch(self, monkeypatch, tmp_path):
        self._saved_shard(tmp_path / "raw", self._today_run_id(2))
        res, calls = self._run(monkeypatch, tmp_path, days=14)
        assert calls == []
        assert len(res.rows) == 1
        row = res.rows[0]
        assert row["description"].startswith("saved description")
        assert row["job_url"] == self.HOST_URL + LIST_ITEM["externalPath"]
        assert row["location"] == "Saved City"
        assert row["found_by_term"] == "term-a"
        assert row["date_posted"] is not None
        assert any("0 fetched, 1 reused" in line for line in res.report_lines)

    def test_a_saved_description_older_than_the_window_is_fetched_again(
            self, monkeypatch, tmp_path):
        self._saved_shard(tmp_path / "raw", self._today_run_id(20))
        res, calls = self._run(monkeypatch, tmp_path, days=14)
        assert len(calls) == 1
        assert "xxx" in res.rows[0]["description"]

    def test_reuse_off_always_fetches(self, monkeypatch, tmp_path):
        self._saved_shard(tmp_path / "raw", self._today_run_id(1))
        _, calls = self._run(monkeypatch, tmp_path, days=0)
        assert len(calls) == 1

    def test_a_blank_saved_description_is_fetched_again(self, monkeypatch, tmp_path):
        self._saved_shard(tmp_path / "raw", self._today_run_id(1), description="  ")
        _, calls = self._run(monkeypatch, tmp_path, days=14)
        assert len(calls) == 1

    def test_another_tenants_posting_is_not_reused(self, monkeypatch, tmp_path):
        raw = tmp_path / "raw"
        raw.mkdir()
        pd.DataFrame([{
            "job_url": "https://other.wd3.myworkdayjobs.com/Site" + LIST_ITEM["externalPath"],
            "title": "x", "location": "", "description": "someone else's",
        }]).to_parquet(raw / f"{self._today_run_id(1)}_workday.parquet")
        _, calls = self._run(monkeypatch, tmp_path, days=14)
        assert len(calls) == 1


class TestRunTrace:
    def test_the_trace_file_records_terms_and_tenants(self, monkeypatch, tmp_path):
        import json

        monkeypatch.setattr(workday, "RUNS_DIR", tmp_path)
        monkeypatch.setattr(workday, "search_terms", lambda cfg: ("term-a",))
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Acme AI", "workday", "acme|wd3|Site")])
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM]})
        _stub_details(monkeypatch)

        ctx = MockContext()
        ctx.run_id = "2026-01-01_2000"
        res = WorkdaySource().fetch(ctx)

        path = tmp_path / "2026-01-01_2000_workday_trace.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        term = next(r for r in records if r["kind"] == "term")
        assert term["term"] == "term-a" and term["stop"] == "short"
        assert term["new_matched"] == 1 and term["pages"] == 1
        tenant = next(r for r in records if r["kind"] == "tenant")
        assert tenant["kept"] == 1 and tenant["kept_by_term"] == {"term-a": 1}
        assert any(line.startswith("HTTP: ") for line in res.report_lines)
        assert any(line.startswith("Trace: ") for line in res.report_lines)

    def test_no_run_id_writes_no_file(self, monkeypatch, tmp_path):
        runs = tmp_path / "runs"
        monkeypatch.setattr(workday, "RUNS_DIR", runs)
        monkeypatch.setattr(universe, "load", lambda ats: [_hot()])
        monkeypatch.setattr(workday, "list_page", lambda *a, **kw: {
            "total": 1, "jobPostings": [LIST_ITEM]})
        _stub_details(monkeypatch)
        WorkdaySource().fetch(MockContext())
        assert not runs.exists()
