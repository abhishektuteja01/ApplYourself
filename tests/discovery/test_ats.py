import pytest
import requests
import pandas as pd
from datetime import date
from src import verticals as verticals_module

_HOT = pd.Timestamp.today().normalize()
from src.discovery import universe
from src.discovery.universe import UniverseCompany as _UniverseCompany


def UniverseCompany(name, ats, slug, priority=False, last_kept_at=_HOT):
    """Hot by default. These tests exercise the shared fetch loop, not the
    hot/cold split — a board with no health is cold, and a two-board cold
    universe rotates one board a run, so the second would never be polled.
    The split itself is tested in test_universe.py."""
    return _UniverseCompany(name, ats, slug, priority, last_kept_at)

# fetch_json and the pacing sleep now live in the shared base, so that is where
# the seam is patched.
from src.discovery.sources.ats import base
from src.discovery.sources.ats.greenhouse import GreenhouseSource
from src.discovery.sources.ats.lever import LeverSource
from src.discovery.sources.ats.ashby import AshbySource
from src import ats_http as http

class MockConfigSources:
    pacing_seconds = 0

class MockContext:
    """Mirrors orchestrator.Context, including `.verticals` — the injected
    synthetic fixture (conftest's autouse fixture) is what the title gate
    reads, same as production."""
    class Config:
        sources = {"greenhouse": MockConfigSources, "lever": MockConfigSources, "ashby": MockConfigSources}
    config = Config
    deadline_ts = 0.0

    @property
    def verticals(self):
        return verticals_module.get_config()

    def deadline_reached(self): return False

def test_greenhouse_rows_shape(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "greenhouse", "acmeai")])
    payload = {"jobs": [{
        "title": "Widget Assembly Consultant",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
        "content": "&lt;p&gt;" + "x" * 250 + "&lt;/p&gt;",
        "location": {"name": "New York, NY"},
        "first_published": "2026-07-01T12:00:00-04:00",
    }]}
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: payload)

    res = GreenhouseSource().fetch(MockContext())
    assert len(res.rows) == 1
    r = res.rows[0]
    assert r["site"] == "greenhouse"
    assert r["company"] == "Acme AI"
    assert r["title"] == "Widget Assembly Consultant"
    assert r["job_url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert r["location"] == "New York, NY"
    assert r["date_posted"] == date(2026, 7, 1)
    assert len(r["description"]) >= 250
    assert r["vertical"] == "example_primary"

def test_greenhouse_rows_skips_incomplete_items(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "greenhouse", "acmeai")])
    payload = {"jobs": [{"title": "", "absolute_url": "https://x"},
                        {"title": "Widget Assembly Consultant"}]}
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: payload)
    assert len(GreenhouseSource().fetch(MockContext()).rows) == 0

def test_lever_rows_assembles_lists_and_maps_salary(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "lever", "acmeai")])
    payload = [{
        "text": "Widget Assembly Consultant",
        "hostedUrl": "https://jobs.lever.co/acme/ab-1",
        "description": "<p>Intro paragraph.</p>",
        "lists": [{"text": "Requirements", "content": "<li>Widgets</li><li>Gizmos</li>"}],
        "descriptionPlain": "should not be used",
        "categories": {"location": "Remote - US", "commitment": "Full-time"},
        "salaryRange": {"min": 150000, "max": 190000, "currency": "USD", "interval": "per-year-salary"},
        "workplaceType": "remote",
        "createdAt": 1780300800000,
    }]
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: payload)
    rows = LeverSource().fetch(MockContext()).rows
    assert len(rows) == 1
    r = rows[0]
    assert r["site"] == "lever"
    assert "Intro paragraph." in r["description"]
    assert "Requirements" in r["description"]
    assert "- Widgets" in r["description"]
    assert "should not be used" not in r["description"]
    assert r["min_amount"] == 150000 and r["max_amount"] == 190000
    assert r["currency"] == "USD"
    assert r["job_type"] == "Full-time"
    assert r["is_remote"] is True
    assert r["location"] == "Remote - US"
    assert r["date_posted"] is not None

def test_lever_rows_falls_back_to_description_plain(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "lever", "acmeai")])
    payload = [{
        "text": "Widget Assembly Consultant",
        "hostedUrl": "https://jobs.lever.co/acme/1",
        "descriptionPlain": "plain body",
    }]
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: payload)
    assert LeverSource().fetch(MockContext()).rows[0]["description"] == "plain body"

def test_ashby_rows_salary_from_compensation_tiers(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "ashby", "acmeai")])
    payload = {"jobs": [{
        "title": "Widget Assembly Consultant",
        "jobUrl": "https://jobs.ashbyhq.com/acme/j1",
        "descriptionHtml": "<p>Body text</p>",
        "location": "San Francisco",
        "isRemote": False,
        "employmentType": "FullTime",
        "publishedAt": "2026-07-10T00:00:00Z",
        "compensation": {"compensationTiers": [{"components": [
            {"compensationType": "Equity", "minValue": 1, "maxValue": 2},
            {"compensationType": "Salary", "minValue": 160000, "maxValue": 200000,
             "currencyCode": "USD", "interval": "1 YEAR"},
        ]}]},
    }]}
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: payload)
    rows = AshbySource().fetch(MockContext()).rows
    assert len(rows) == 1
    r = rows[0]
    assert r["site"] == "ashby"
    assert (r["min_amount"], r["max_amount"], r["currency"]) == (160000, 200000, "USD")
    assert r["is_remote"] is False
    assert r["job_type"] == "FullTime"
    assert r["date_posted"] == date(2026, 7, 10)
    assert r["description"] == "Body text"



def test_scrape_boards_isolates_per_company_failures(monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda _: None)
    monkeypatch.setattr(universe, "load", lambda ats: [
        UniverseCompany("Broken Co", "greenhouse", "badslug"),
        UniverseCompany("Acme AI", "greenhouse", "acmeai"),
    ])
    def fake_fetch(url, **kw):
        if "badslug" in url:
            raise http.CareersError("board not found (404)", status=404, permanent=True)
        return {"jobs": [{"title": "Widget Assembly Consultant", "absolute_url": "https://x/1",
                          "content": "a" * 250}]}
    monkeypatch.setattr(base, "fetch_json", fake_fetch)
    res = GreenhouseSource().fetch(MockContext())
    assert len(res.errors) == 1
    assert "Broken Co" in res.errors[0]
    assert len(res.rows) == 1

def _two_job_board(n_off_lane=0):
    """One gate-passing title, one that classifies and then trips a
    title_exclude_term, optionally one that classifies as nothing at all."""
    jobs = [
        {"title": "Widget Assembly Consultant", "absolute_url": "https://x/1",
         "content": "a" * 250},
        {"title": "Senior Widget Consultant", "absolute_url": "https://x/2",
         "content": "a" * 250},
    ]
    jobs += [{"title": "Definitely Not A Match Zzz", "absolute_url": "https://x/3",
              "content": "a" * 250}] * n_off_lane
    return {"jobs": jobs}


def test_a_gate_failing_title_never_reaches_the_shard(monkeypatch):
    """Cleaning's `apply_title_exclusion` runs here too, not only hours later:
    no HTTP saved on a single-call board, but the raw shard stays ~5x smaller."""
    monkeypatch.setattr(universe, "load",
                        lambda ats: [UniverseCompany("Acme AI", "greenhouse", "acmeai")])
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: _two_job_board())

    rows = GreenhouseSource().fetch(MockContext()).rows
    assert [r["title"] for r in rows] == ["Widget Assembly Consultant"]


def test_health_records_the_kept_count_not_the_fetched_count(monkeypatch):
    """`universe.load` sorts by `last_yield`, so it has to mean "postings
    relevant to this profile", not "postings on the board"."""
    monkeypatch.setattr(universe, "load",
                        lambda ats: [UniverseCompany("Acme AI", "greenhouse", "acmeai")])
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: _two_job_board(n_off_lane=1))
    recorded = []
    monkeypatch.setattr(universe.HealthLedger, "mark_ok",
                        lambda self, slug, kept=0: recorded.append((True, kept)))

    res = GreenhouseSource().fetch(MockContext())
    assert len(res.rows) == 1
    assert recorded == [(True, 1)]


class TestMalformedPayloadStaysPerCompany:
    """fetch_json returns whatever a 200 decodes to. Before the row parse moved
    inside the try, a list or a non-dict item raised AttributeError out of the
    company loop; the orchestrator caught it at *source* level and wrote no
    shard at all, discarding every company polled before the bad one."""

    GOOD = {"jobs": [{"title": "Widget Assembly Consultant",
                      "absolute_url": "https://x/1", "content": "a" * 250}]}

    @pytest.mark.parametrize("bad", [
        [],                              # a bare list
        [{"title": "x"}],                # a list of dicts
        "not json at all",               # a JSON string
        {"error": "unauthorized"},       # right type, no jobs key
        {"jobs": "not a list"},
        {"jobs": [None, 42, "str"]},     # non-dict items
        123,
    ], ids=["empty_list", "list_of_dicts", "json_string", "error_object",
            "jobs_not_list", "non_dict_items", "int"])
    def test_bad_payload_does_not_lose_the_other_companies(self, monkeypatch, bad):
        monkeypatch.setattr(base.time, "sleep", lambda _: None)
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Broken Co", "greenhouse", "badslug"),
            UniverseCompany("Acme AI", "greenhouse", "acmeai"),
        ])
        monkeypatch.setattr(base, "fetch_json",
                            lambda url, **kw: bad if "badslug" in url else self.GOOD)
        res = GreenhouseSource().fetch(MockContext())
        # The healthy company's rows survive no matter what the bad one returned.
        assert len(res.rows) == 1
        assert res.rows[0]["company"] == "Acme AI"

    def test_lever_survives_a_dict_payload(self, monkeypatch):
        monkeypatch.setattr(base.time, "sleep", lambda _: None)
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Broken Co", "lever", "badslug"),
            UniverseCompany("Acme AI", "lever", "acmeai"),
        ])
        good = [{"text": "Widget Assembly Consultant",
                 "hostedUrl": "https://x/1", "descriptionPlain": "a" * 250}]
        monkeypatch.setattr(base, "fetch_json", lambda url, **kw:
                            {"error": "nope"} if "badslug" in url else good)
        res = LeverSource().fetch(MockContext())
        assert len(res.rows) == 1

    def test_every_company_failing_the_same_way_is_raised_not_swallowed(self, monkeypatch):
        """A shape error on one company is bad data. On all of them it is a bug
        in parse_rows, and containing it would hand the orchestrator a valid
        empty shard instead of failing loud."""
        monkeypatch.setattr(base.time, "sleep", lambda _: None)
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("A Co", "greenhouse", "a"),
            UniverseCompany("B Co", "greenhouse", "b"),
        ])
        monkeypatch.setattr(base, "fetch_json", lambda url, **kw: "not an object")
        with pytest.raises(TypeError):
            GreenhouseSource().fetch(MockContext())

    def test_a_shape_error_is_reported_as_a_named_company_error(self, monkeypatch):
        monkeypatch.setattr(base.time, "sleep", lambda _: None)
        monkeypatch.setattr(universe, "load", lambda ats: [
            UniverseCompany("Broken Co", "greenhouse", "badslug")])
        monkeypatch.setattr(base, "fetch_json", lambda url, **kw: object())
        res = GreenhouseSource().fetch(MockContext())
        assert len(res.errors) == 1
        assert "Broken Co" in res.errors[0]
        assert "malformed board payload" in res.errors[0]


def test_scrape_boards_isolates_non_json_body(monkeypatch):
    """An HTML 200 must stay a per-company error, not escape the company loop."""
    monkeypatch.setattr(base.time, "sleep", lambda _: None)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    monkeypatch.setattr(universe, "load", lambda ats: [
        UniverseCompany("Wall Co", "greenhouse", "wallslug"),
        UniverseCompany("Acme AI", "greenhouse", "acmeai"),
    ])
    def fake_get(url, timeout=None, headers=None):
        if "wallslug" in url:
            return _HtmlResponse()
        return _JsonResponse({"jobs": [{"title": "Widget Assembly Consultant",
                                        "absolute_url": "https://x/1",
                                        "content": "a" * 250}]})
    monkeypatch.setattr(http.requests, "get", fake_get)
    res = GreenhouseSource().fetch(MockContext())
    assert len(res.errors) == 1
    assert "Wall Co" in res.errors[0] and "invalid JSON body" in res.errors[0]
    assert len(res.rows) == 1

class _HtmlResponse:
    status_code = 200
    headers: dict = {}
    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)


class _StatusResponse:
    headers: dict = {}
    def __init__(self, status_code): self.status_code = status_code
    def json(self): return {}


class TestHealthLedgerOnlyCountsDeadBoards:
    """A prune benches a board for 14 days, so only a permanently dead board may
    earn a strike. Transient failures must leave the ledger untouched."""

    @pytest.fixture
    def strikes(self, monkeypatch):
        """Run one company through the real fetch_json, then read its ledger row."""
        monkeypatch.setattr(http.time, "sleep", lambda _: None)
        monkeypatch.setattr(universe, "load",
                            lambda ats: [UniverseCompany("Acme AI", "greenhouse", "acmeai")])

        def run(response_factory):
            monkeypatch.setattr(http.requests, "get",
                                lambda url, timeout=None, headers=None: response_factory())
            res = GreenhouseSource().fetch(MockContext())
            if not universe.health_path("greenhouse").exists():
                return res, None
            df = pd.read_parquet(universe.health_path("greenhouse"))
            return res, df.iloc[0]

        return run

    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    def test_transient_status_writes_no_ledger_row(self, strikes, status):
        res, row = strikes(lambda: _StatusResponse(status))
        assert row is None
        assert "404: 0 | Err: 1" in res.report_lines[0]

    def test_connection_error_writes_no_ledger_row(self, strikes):
        def boom(): raise requests.ConnectionError("reset")
        _, row = strikes(boom)
        assert row is None

    def test_404_earns_a_strike(self, strikes):
        res, row = strikes(lambda: _StatusResponse(404))
        assert row["consecutive_404s"] == 1
        assert "404: 1 | Err: 0" in res.report_lines[0]

    def test_non_json_200_earns_a_strike(self, strikes):
        """A wall or a wrong slug is permanent, but it is not a 404."""
        res, row = strikes(_HtmlResponse)
        assert row["consecutive_404s"] == 1
        assert "404: 0 | Err: 1" in res.report_lines[0]

    def test_slug_containing_404_is_not_a_dead_board(self, monkeypatch):
        """Regression: `"404" in str(e)` read the URL, not the status code."""
        monkeypatch.setattr(http.time, "sleep", lambda _: None)
        monkeypatch.setattr(universe, "load",
                            lambda ats: [UniverseCompany("Acme", "greenhouse", "acme-404")])
        monkeypatch.setattr(http.requests, "get",
                            lambda url, timeout=None, headers=None: _StatusResponse(500))
        res = GreenhouseSource().fetch(MockContext())
        assert not universe.health_path("greenhouse").exists()
        assert "404: 0 | Err: 1" in res.report_lines[0]

class _JsonResponse:
    status_code = 200
    headers: dict = {}
    def __init__(self, payload): self._payload = payload
    def json(self): return self._payload


def test_deadline_break_still_flushes_the_health_ledger(monkeypatch):
    """D2.4: health is batched now, so a run cut by the deadline would lose
    everything it learned unless the break flushes."""
    monkeypatch.setattr(universe, "load", lambda ats: [
        UniverseCompany("Acme AI", "greenhouse", "acmeai"),
        UniverseCompany("Beta Co", "greenhouse", "beta"),
    ])
    monkeypatch.setattr(base, "fetch_json", lambda url, **kw: _two_job_board())

    class CutAfterFirst(MockContext):
        polled = 0

        def deadline_reached(self):
            hit = self.polled >= 1
            self.polled += 1
            return hit

    GreenhouseSource().fetch(CutAfterFirst())
    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert list(df["slug"]) == ["acmeai"]


# ---------------------------------------------------------------------
# registry.py — the one board table discovery and src/apply both read
# ---------------------------------------------------------------------

from src.discovery.sources.ats import registry  # noqa: E402

_UUID = "12345678-abcd-4bcd-8bcd-1234567890ab"


class TestRegistryBoardSlug:
    @pytest.mark.parametrize("url,slug", [
        ("https://job-boards.greenhouse.io/acme/jobs/4567", "acme"),
        ("https://boards.greenhouse.io/embed/job_app/acme/jobs/4567", "acme"),
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=99", "acme"),
        (f"https://jobs.lever.co/acme/{_UUID}", "acme"),
        (f"https://jobs.ashbyhq.com/acme/{_UUID}", "acme"),
        # Workday's tenant is the first host label, not a path segment.
        ("https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/US-CA/Eng_JR1", "acme"),
        ("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs", "acme"),
    ])
    def test_slug(self, url, slug):
        assert registry.board_slug(url) == slug

    def test_non_board_url_has_no_slug(self):
        assert registry.board_slug("https://www.linkedin.com/jobs/view/1") == ""
        assert registry.board_slug("") == ""

    def test_workday_pod_and_site_id(self):
        hit = registry.parse_posting_url(
            "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/US-CA/Eng_JR1")
        assert (hit.source, hit.slug, hit.pod, hit.site_id, hit.posting_id) == \
            ("workday", "acme", "wd5", "AcmeCareers", "Eng_JR1")


class TestRegistryDetectSource:
    def test_eu_lever_is_plain_lever(self):
        # region never rides along in the source name
        assert registry.detect_source(f"https://jobs.eu.lever.co/acme/{_UUID}") == "lever"
        assert registry.parse_posting_url(
            f"https://jobs.eu.lever.co/acme/{_UUID}").region == "eu"

    def test_eu_greenhouse_hosts(self):
        for host in ("boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"):
            assert registry.detect_source(f"https://{host}/acme/jobs/1") == "greenhouse"

    def test_gh_jid_on_an_arbitrary_careers_host(self):
        assert registry.detect_source(
            "https://careers.acme.com/jobs?gh_jid=8044460") == "greenhouse"

    def test_two_different_gh_jids_are_unresolvable(self):
        assert registry.detect_source(
            "https://careers.acme.com/jobs?gh_jid=1&gh_jid=2") is None
        # the same id twice is real and does resolve
        assert registry.detect_source(
            "https://careers.acme.com/jobs?gh_jid=1&gh_jid=1") == "greenhouse"

    def test_workday_is_detected_but_not_submittable(self):
        assert registry.detect_source(
            "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/US/X_JR1") == "workday"
        assert "workday" not in registry.SUBMITTABLE_SOURCES
        assert "workday" not in registry.DRIVER_NAMES

    def test_aggregator_is_not_a_board(self):
        assert registry.detect_source("https://www.linkedin.com/jobs/view/1") is None


class TestRegistryIsApplyable:
    def test_lookalike_host_is_not_a_board(self):
        # substring-over-URL matching would call this Greenhouse
        assert not registry.is_applyable("https://evilgreenhouse.io.example.com/acme/jobs/1")
        assert not registry.is_applyable("https://example.com/?x=greenhouse.io")

    def test_board_host_without_a_posting_id_still_counts(self):
        assert registry.is_applyable("https://boards.greenhouse.io/acme")

    def test_gh_jid_careers_page_counts(self):
        assert registry.is_applyable("https://stripe.com/jobs/search?gh_jid=8044460")

    def test_aggregator_does_not(self):
        assert not registry.is_applyable("https://www.linkedin.com/jobs/view/1")

    def test_markers_stay_exported_for_back_compat(self):
        assert registry.ATS_URL_MARKERS == (
            "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com")
        assert registry.ATS_SOURCE_NAMES == (
            "greenhouse", "lever", "ashby", "workday")


def test_the_ledger_sees_the_whole_universe_not_tonights_slice(monkeypatch, tmp_path):
    """The trap the split introduces: HealthLedger.flush prunes every slug it
    was not told about, so handing it the polled slice would delete the health
    of every cold board this run happens not to visit."""
    monkeypatch.setattr(base.time, "sleep", lambda _: None)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)

    cold = [_UniverseCompany("Co %d" % i, "greenhouse", "cold%02d" % i) for i in range(14)]
    monkeypatch.setattr(universe, "load", lambda ats: list(cold))

    # Seed health for every cold board, as a run before the split would have.
    seeded = universe.HealthLedger("greenhouse", [c.slug for c in cold])
    for c in cold:
        seeded.mark_ok(c.slug, 0)
    seeded.flush()

    monkeypatch.setattr(http.requests, "get",
                        lambda url, timeout=None, headers=None: _JsonResponse({"jobs": []}))
    GreenhouseSource().fetch(MockContext())

    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert len(df) == 14, "an unvisited cold board lost its health row"


def test_the_cold_rotation_advances_across_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(base.time, "sleep", lambda _: None)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)

    cold = [_UniverseCompany("Co %d" % i, "greenhouse", "cold%02d" % i) for i in range(14)]
    monkeypatch.setattr(universe, "load", lambda ats: list(cold))

    polled = []

    def fake_get(url, timeout=None, headers=None):
        polled.append(url)
        return _JsonResponse({"jobs": []})

    monkeypatch.setattr(http.requests, "get", fake_get)

    GreenhouseSource().fetch(MockContext())
    first = list(polled)
    polled.clear()
    GreenhouseSource().fetch(MockContext())

    assert len(first) == 2 and len(polled) == 2
    assert not set(first) & set(polled)


def test_the_summary_names_the_universe_not_just_the_slice(monkeypatch, tmp_path):
    monkeypatch.setattr(base.time, "sleep", lambda _: None)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(universe, "load", lambda ats: (
        [UniverseCompany("Hot Co", "greenhouse", "hot")]
        + [_UniverseCompany("Co %d" % i, "greenhouse", "cold%02d" % i) for i in range(14)]))
    monkeypatch.setattr(http.requests, "get",
                        lambda url, timeout=None, headers=None: _JsonResponse({"jobs": []}))

    res = GreenhouseSource().fetch(MockContext())

    assert res.report_lines[0].startswith(
        "Companies polled: 3 of 15 (1 hot, 2 of 14 cold)")
