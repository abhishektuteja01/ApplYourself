"""The four board adapters against recorded payload shapes. No network: the
fetchers are replaced."""

from __future__ import annotations

import pandas as pd
import pytest

from applyourself.discovery import store as S
from applyourself.discovery.config import DiscoveryConfig
from applyourself.discovery.sources.boards import ashby, eightfold, greenhouse, lever, workday
from applyourself.discovery.sources.boards import base as B
from applyourself.shared.http_client import CareersError

TODAY = pd.Timestamp("2026-09-27")
HTML = "<p>" + "Build and ship retrieval systems for clinical trial data. " * 6 + "</p>"
BOARD = B.Board("greenhouse", "Acme", "acme")


def crawl(cfg_v, tmp_path):
    c = B.Crawl(
        DiscoveryConfig(),
        cfg_v,
        TODAY,
        0.0,
        {},
        S.Batch(),
        board_state=tmp_path / "b.parquet",
        boards_dir=tmp_path,
    )
    return c


def test_greenhouse_lists_without_content_and_fetches_detail(monkeypatch, cfg, tmp_path):
    calls = []

    def fake(url, deadline_ts=None):
        calls.append(url)
        if url.endswith("/jobs"):
            return {
                "jobs": [
                    {
                        "id": 7,
                        "title": "AI Engineer",
                        "updated_at": "2026-09-20T10:00:00Z",
                        "first_published": "2026-09-01T10:00:00Z",
                        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7",
                        "location": {"name": "Austin, TX"},
                    }
                ]
            }
        return {
            "id": 7,
            "title": "AI Engineer",
            "content": HTML,
            "updated_at": "2026-09-20T10:00:00Z",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7",
            "location": {"name": "Austin, TX"},
        }

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    lane = greenhouse.GreenhouseLane()
    c = crawl(cfg, tmp_path)
    [p] = lane.list_board(BOARD, c, lambda: None)
    assert "content" not in calls[0] and p["jd_text"] == "" and p["posting_key"] == "greenhouse|7"
    full = lane.detail(BOARD, {**p, "vertical": "primary"}, c)
    assert calls[1].endswith("/jobs/7") and "retrieval" in full["jd_text"]
    assert full["posted_date"].isoformat() == "2026-09-01"


def test_greenhouse_stale_only_when_the_stored_copy_is_dated_and_differs():
    lane = greenhouse.GreenhouseLane()
    p = {"updated_at": "b"}
    assert lane.stale(p, {"updated_at": "a"})
    assert not lane.stale(p, {"updated_at": "b"}) and not lane.stale(p, {"updated_at": ""})


def test_lever_parses_sections_salary_and_remote():
    payload = [
        {
            "text": "ML Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/1234abcd-0000",
            "description": HTML,
            "lists": [{"text": "You will", "content": "<li>ship</li>"}],
            "categories": {"location": "Remote", "commitment": "Full-time"},
            "workplaceType": "remote",
            "createdAt": 1790000000000,
            "salaryRange": {"min": 100, "max": 200, "currency": "USD"},
        }
    ]
    [p] = lever.postings_from(payload, "Acme")
    assert p["remote_flag"] and p["salary_min"] == 100 and "## You will" in p["jd_text"]
    with pytest.raises(TypeError):
        lever.postings_from({"error": 1}, "Acme")


def test_an_hourly_salary_is_stored_yearly():
    payload = [
        {
            "text": "ML Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/1234abcd-0000",
            "description": HTML,
            "salaryRange": {"min": 80, "max": 110, "currency": "USD", "interval": "per-hour-wage"},
        }
    ]
    [p] = lever.postings_from(payload, "Acme")
    assert (p["salary_min"], p["salary_max"]) == (166_400, 228_800)


def test_a_search_row_with_no_remote_value_is_not_remote():
    from applyourself.discovery.sources.search import posting_from

    row = posting_from(
        {
            "job_url": "https://www.indeed.com/viewjob?jk=abc",
            "title": "AI Engineer",
            "company": "Acme",
            "is_remote": float("nan"),
        },
        "indeed",
        "primary",
        "t",
        False,
    )
    assert row["remote_flag"] is False


def test_ashby_falls_back_to_graphql_on_404(monkeypatch, cfg, tmp_path):
    def rest(slug, company, deadline_ts=None):
        raise CareersError("nope", status=404, permanent=True)

    monkeypatch.setattr(ashby, "list_rest", rest)
    monkeypatch.setattr(
        ashby,
        "graphql_board",
        lambda slug, deadline_ts=None: [
            {
                "id": "abc12345-0000",
                "title": "AI Engineer",
                "locationName": "NYC",
                "workplaceType": "Remote",
            }
        ],
    )
    [p] = ashby.AshbyLane().list_board(
        B.Board("ashby", "Acme", "acme"), crawl(cfg, tmp_path), lambda: None
    )
    assert p["jd_text"] == "" and p["_id"] == "abc12345-0000" and p["remote_flag"]


def test_ashby_short_rest_jd_fetches_detail_by_id(monkeypatch, cfg, tmp_path):
    sent = {}

    def fake(op, query, variables, deadline_ts=None):
        sent.update(variables)
        return {"jobPosting": {"descriptionHtml": HTML, "publishedDate": "2026-09-20"}}

    monkeypatch.setattr(ashby, "graphql", fake)
    p = ashby.posting_from(
        {
            "id": "abc12345-0000",
            "title": "AI Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/acme/abc12345-0000",
            "descriptionPlain": "Short.",
        },
        "Acme",
    )
    full = ashby.AshbyLane().detail(B.Board("ashby", "Acme", "acme"), p, crawl(cfg, tmp_path))
    assert sent["jobPostingId"] == "abc12345-0000" and len(full["jd_text"]) >= 200


def test_ashby_a_missing_board_stays_a_404(monkeypatch, cfg, tmp_path):
    def rest(slug, company, deadline_ts=None):
        raise CareersError("nope", status=404, permanent=True)

    monkeypatch.setattr(ashby, "list_rest", rest)
    monkeypatch.setattr(ashby, "graphql_board", lambda slug, deadline_ts=None: None)
    with pytest.raises(CareersError):
        ashby.AshbyLane().list_board(
            B.Board("ashby", "Acme", "acme"), crawl(cfg, tmp_path), lambda: None
        )


def test_workday_pages_until_nothing_passes(monkeypatch, cfg, tmp_path):
    pages = []

    def fake(company, wd, site_id, offset, search_text="", deadline_ts=None):
        pages.append((search_text, offset))
        if offset == 0:
            items = [
                {
                    "title": "Widget Functional Consultant",
                    "externalPath": f"/job/X/Widget-Consultant_R{i}",
                }
                for i in range(20)
            ]
        else:
            items = [
                {"title": "Warehouse Associate", "externalPath": f"/job/X/W_R{offset + i}"}
                for i in range(20)
            ]
        return {"jobPostings": items}

    monkeypatch.setattr(workday, "list_page", fake)
    c = crawl(cfg, tmp_path)
    out = workday.WorkdayLane().list_board(
        B.Board("workday", "Acme", "acme|wd5|External"), c, lambda *_: None
    )
    terms = B.search_terms(cfg)
    assert [o for t, o in pages if t == terms[0]] == [0, 20]
    assert out[0]["posting_key"] == "workday|acme|widget-consultant_r0"


def test_workday_rejects_a_bad_slug():
    with pytest.raises(workday.WorkdaySlugError):
        workday.parse_slug("acme|x5|site")


def _ef_page(n, title="Widget Functional Consultant", start=0):
    return [
        {
            "id": start + i,
            "name": title,
            "locations": ["Austin, TX, US"],
            "postedTs": 1790208000,
            "positionUrl": f"/careers/job/{start + i}",
        }
        for i in range(n)
    ]


def test_eightfold_pages_until_a_short_page(monkeypatch, cfg, tmp_path):
    pages = []

    def fake(host, domain, api, term, start, deadline_ts=None):
        pages.append((term, start))
        return _ef_page(10 if start == 0 else 3, start=start), 13

    monkeypatch.setattr(eightfold, "list_page", fake)
    out = eightfold.EightfoldLane().list_board(
        B.Board("eightfold", "Acme", "acme.eightfold.ai|acme.com"),
        crawl(cfg, tmp_path),
        lambda: None,
    )
    terms = B.search_terms(cfg)
    assert [s for t, s in pages if t == terms[0]] == [0, 10]
    assert out[0]["url"] == "https://acme.eightfold.ai/careers/job/0"
    assert out[0]["_api"] == "pcsx" and out[0]["posted_date"] is not None
    assert out[0]["location_raw"] == "Austin, TX, US"


def test_eightfold_falls_back_to_v2_when_pcsx_is_refused(monkeypatch, cfg, tmp_path):
    def fake(host, domain, api, term, start, deadline_ts=None):
        if api == "pcsx":
            raise CareersError("PCSX is not enabled", status=403)
        return [
            {
                "id": 9,
                "name": "Widget AI Engineer",
                "location": "Austin, TX",
                "canonicalPositionUrl": "https://careers.acme.net/careers/job/9",
                "t_create": 1790208000,
                "job_description": "",
            }
        ], 1

    monkeypatch.setattr(eightfold, "list_page", fake)
    [p] = eightfold.EightfoldLane().list_board(
        B.Board("eightfold", "Acme", "careers.acme.net|acme.com"),
        crawl(cfg, tmp_path),
        lambda: None,
    )
    assert p["_api"] == "v2" and p["url"] == "https://careers.acme.net/careers/job/9"
    assert p["location_raw"] == "Austin, TX"


def test_eightfold_other_errors_are_not_a_fallback(monkeypatch, cfg, tmp_path):
    def fake(host, domain, api, term, start, deadline_ts=None):
        raise CareersError("down", status=503)

    monkeypatch.setattr(eightfold, "list_page", fake)
    with pytest.raises(CareersError):
        eightfold.EightfoldLane().list_board(
            B.Board("eightfold", "Acme", "acme.eightfold.ai|acme.com"),
            crawl(cfg, tmp_path),
            lambda: None,
        )


@pytest.mark.parametrize(
    "api, payload, url_part",
    [
        (
            "pcsx",
            {"data": {"jobDescription": HTML}},
            "/api/pcsx/position_details?position_id=7&domain=acme.com",
        ),
        ("v2", {"job_description": HTML}, "/api/apply/v2/jobs/7?domain=acme.com"),
    ],
)
def test_eightfold_detail_reads_the_jd(monkeypatch, cfg, tmp_path, api, payload, url_part):
    sent = []

    def fake(url, deadline_ts=None):
        sent.append(url)
        return payload

    monkeypatch.setattr(eightfold, "fetch_json", fake)
    p = eightfold.posting_from(
        {"id": 7, "name": "AI Engineer"}, "acme.eightfold.ai", api, "Acme", "AI"
    )
    full = eightfold.EightfoldLane().detail(
        B.Board("eightfold", "Acme", "acme.eightfold.ai|acme.com"), p, crawl(cfg, tmp_path)
    )
    assert url_part in sent[0] and len(full["jd_text"]) >= 200


def test_eightfold_rejects_a_bad_slug():
    with pytest.raises(eightfold.EightfoldSlugError):
        eightfold.parse_slug("acme.eightfold.ai")
