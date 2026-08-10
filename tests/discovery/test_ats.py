import pytest
import requests
import pandas as pd
from datetime import date
from src.discovery.htmlutil import html_to_text
from src.discovery import universe
from src.discovery.universe import UniverseCompany
# fetch_json and the pacing sleep now live in the shared base, so that is where
# the seam is patched.
from src.discovery.sources.ats import base
from src.discovery.sources.ats.greenhouse import GreenhouseSource
from src.discovery.sources.ats.lever import LeverSource
from src.discovery.sources.ats.ashby import AshbySource
from src.discovery.sources.ats import http

def test_html_to_text_strips_tags_and_keeps_structure():
    html = "<div><h2>Requirements</h2><ul><li>Widgets</li><li>Gizmos</li></ul></div>"
    text = html_to_text(html)
    assert "## Requirements" in text
    assert "- Widgets" in text
    assert "- Gizmos" in text
    assert "<" not in text

def test_html_to_text_handles_greenhouse_double_encoding():
    encoded = "&lt;p&gt;Build &amp;amp; ship widgets&lt;/p&gt;"
    assert html_to_text(encoded) == "Build & ship widgets"

def test_html_to_text_non_string_is_empty():
    assert html_to_text(None) == ""
    assert html_to_text("   ") == ""

class MockConfigSources:
    pacing_seconds = 0

class MockContext:
    class Config:
        sources = {"greenhouse": MockConfigSources, "lever": MockConfigSources, "ashby": MockConfigSources}
    config = Config
    deadline_ts = 0.0
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
