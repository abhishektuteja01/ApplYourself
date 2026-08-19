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
    def test_returns_exactly_one_term(self):
        terms = search_terms(verticals_module.get_config())
        assert len(terms) == 1

    def test_the_term_is_the_default_verticals_first_search_term(self):
        cfg = verticals_module.get_config()
        default = cfg.verticals[cfg.default_vertical]
        assert search_terms(cfg) == (default.search_terms[0],)


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

    def _companies(self, n=6):
        return [UniverseCompany(f"Co {i}", "workday", f"c{i}|wd1|Site") for i in range(n)]

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
        companies = self._companies(1)
        full = [dict(LIST_ITEM, externalPath=f"/job/{i}") for i in range(workday.LIST_LIMIT)]
        calls: list[int] = []

        def always_full(company, wd, site_id, offset, search_text="", deadline_ts=None):
            calls.append(offset)
            return {"total": 0, "jobPostings": full}

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

        companies = self._companies(1)
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
