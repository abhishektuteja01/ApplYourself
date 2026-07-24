import pytest
from datetime import date
from src.discovery.htmlutil import html_to_text
from src.discovery import universe
from src.discovery.universe import UniverseCompany
from src.discovery.sources.ats import greenhouse, lever, ashby
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
    def deadline_reached(self): return False

def test_greenhouse_rows_shape(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "greenhouse", "acmeai")])
    payload = {"jobs": [{
        "title": "SAP ACM Consultant",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
        "content": "&lt;p&gt;" + "x" * 250 + "&lt;/p&gt;",
        "location": {"name": "New York, NY"},
        "first_published": "2026-07-01T12:00:00-04:00",
    }]}
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url: payload)
    
    res = GreenhouseSource().fetch(MockContext())
    assert len(res.rows) == 1
    r = res.rows[0]
    assert r["site"] == "greenhouse"
    assert r["company"] == "Acme AI"
    assert r["title"] == "SAP ACM Consultant"
    assert r["job_url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert r["location"] == "New York, NY"
    assert r["date_posted"] == date(2026, 7, 1)
    assert len(r["description"]) >= 250
    assert r["vertical"] == "sap"

def test_greenhouse_rows_skips_incomplete_items(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "greenhouse", "acmeai")])
    payload = {"jobs": [{"title": "", "absolute_url": "https://x"},
                        {"title": "SAP ACM Consultant"}]}
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url: payload)
    assert len(GreenhouseSource().fetch(MockContext()).rows) == 0

def test_lever_rows_assembles_lists_and_maps_salary(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "lever", "acmeai")])
    payload = [{
        "text": "SAP ACM Consultant",
        "hostedUrl": "https://jobs.lever.co/acme/ab-1",
        "description": "<p>Intro paragraph.</p>",
        "lists": [{"text": "Requirements", "content": "<li>Widgets</li><li>Gizmos</li>"}],
        "descriptionPlain": "should not be used",
        "categories": {"location": "Remote - US", "commitment": "Full-time"},
        "salaryRange": {"min": 150000, "max": 190000, "currency": "USD", "interval": "per-year-salary"},
        "workplaceType": "remote",
        "createdAt": 1780300800000,
    }]
    monkeypatch.setattr(lever, "fetch_json", lambda url: payload)
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
        "text": "SAP ACM Consultant",
        "hostedUrl": "https://jobs.lever.co/acme/1",
        "descriptionPlain": "plain body",
    }]
    monkeypatch.setattr(lever, "fetch_json", lambda url: payload)
    assert LeverSource().fetch(MockContext()).rows[0]["description"] == "plain body"

def test_ashby_rows_salary_from_compensation_tiers(monkeypatch):
    monkeypatch.setattr(universe, "load", lambda ats: [UniverseCompany("Acme AI", "ashby", "acmeai")])
    payload = {"jobs": [{
        "title": "SAP ACM Consultant",
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
    monkeypatch.setattr(ashby, "fetch_json", lambda url: payload)
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
    monkeypatch.setattr(universe, "load", lambda ats: [
        UniverseCompany("Broken Co", "greenhouse", "badslug"),
        UniverseCompany("Acme AI", "greenhouse", "acmeai"),
    ])
    def fake_fetch(url):
        if "badslug" in url:
            raise http.CareersError("board not found (404)")
        return {"jobs": [{"title": "SAP ACM Consultant", "absolute_url": "https://x/1",
                          "content": "a" * 250}]}
    monkeypatch.setattr(greenhouse, "fetch_json", fake_fetch)
    res = GreenhouseSource().fetch(MockContext())
    assert len(res.errors) == 1
    assert "Broken Co" in res.errors[0]
    assert len(res.rows) == 1
