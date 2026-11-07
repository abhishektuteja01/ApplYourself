"""The stapply feed lane against a small file in the feed's own shape. No
network: the manifest, the download and page fetches are replaced."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from applyourself.discovery import store as S
from applyourself.discovery.config import DiscoveryConfig
from applyourself.discovery.sources.boards import ashby, greenhouse, lever, workday
from applyourself.discovery.sources.boards import base as B
from applyourself.discovery.sources.feed import arbeitsagentur, oracle, stapply

TODAY = pd.Timestamp("2026-10-23")
JD = "Build and ship retrieval systems for clinical trial data. " * 6
GH = "https://job-boards.greenhouse.io/ddome/jobs/{}"
WD = "https://acme.wd5.myworkdayjobs.com/External/job/Berlin/AI-Engineer_R{}"


def row(**kw):
    base = {
        "url": GH.format(1),
        "title": "Cog Engineer",
        "company": "ddome",
        "ats_type": "greenhouse",
        "location": "Paris, France",
        "country_iso": "FR",
        "is_remote": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "employment_type": "FULL_TIME",
        "description": JD,
        "posted_at": "2026-10-20T10:00:00",
        "raw": "{}",
    }
    return {**base, **kw}


ROWS = [
    row(),
    row(
        url="https://europa.eu/eures/portal/jv-se/jv-details/abc?lang=en",
        ats_type="eures",
        company="siehe Beschreibung",
        location="DE (DE91C)",
        country_iso="DE",
    ),
    row(
        url="https://jobs.lever.co/acme/1234abcd-0000-0000-0000-000000000000",
        ats_type="lever",
        company="acme",
        is_remote="false",
        salary_min="80.0",
        salary_max="110.0",
        salary_currency="USD",
        salary_period="HOUR",
        location="Austin, TX",
        country_iso="CA",
    ),
    row(
        url="https://www.ycombinator.com/companies/clip/jobs/x-ai-engineer",
        ats_type="ycombinator",
        company="Clip",
        description="Short.",
        location="San Francisco, CA, US",
        country_iso="US",
    ),
    row(url=WD.format(1), ats_type="workday", company="Acme", title="Barista", location="Berlin"),
    row(
        url="https://jobs.ashbyhq.com/other/0000abcd-0000-0000-0000-000000000000",
        ats_type="ashby",
        company="other",
        title="Store Manager",
    ),
]


@pytest.fixture
def feed(tmp_path):
    path = tmp_path / "feed.parquet"
    pd.DataFrame(ROWS).to_parquet(path, row_group_size=2)
    (tmp_path / "boards.csv").write_text(
        "ats,name,slug,active\ngreenhouse,DataDome,ddome,\nworkday,Acme,acme|wd5|External,\n",
        encoding="utf-8",
    )
    return path


def make_crawl(vcfg, tmp_path, known=None, cfg=None):
    return B.Crawl(
        cfg or DiscoveryConfig(),
        vcfg,
        TODAY,
        0.0,
        known or {},
        S.Batch(),
        board_state=tmp_path / "b.parquet",
        boards_dir=tmp_path,
    )


def read(vcfg, tmp_path, feed, known=None):
    lane, crawl = stapply.StapplyLane(), make_crawl(vcfg, tmp_path, known)
    lane.read(feed, crawl)
    return lane, crawl


def test_only_rows_that_pass_the_title_gate_are_kept(cfg, tmp_path, feed):
    lane, _ = read(cfg, tmp_path, feed)
    assert lane.stats.listed == 6
    assert [r["title"] for r in lane.rows] == ["Cog Engineer"] * 4
    # every drop is counted with its reason and a sample title, not stored
    assert lane.stats.drops == {"no_lane": 2}
    assert lane.stats.drop_samples == {"no_lane": ["Barista", "Store Manager"]}


def test_a_known_board_keeps_our_name_and_a_placeholder_is_no_company(cfg, tmp_path, feed):
    lane, _ = read(cfg, tmp_path, feed)
    by_url = {r["url"]: r for r in lane.rows}
    assert by_url[GH.format(1)]["company"] == "DataDome"
    assert by_url[GH.format(1)]["board"] == "greenhouse|ddome"
    eures = by_url["https://europa.eu/eures/portal/jv-se/jv-details/abc?lang=en"]
    assert eures["company"] == "" and eures["company_normalized"] == ""


FEED_NAMES = stapply.company_names(
    pd.DataFrame(
        [
            {
                "ats": "lever",
                "name": "Acme Robotics",
                "slug": "acme",
                "url": "https://jobs.lever.co/acme",
            },
            {"ats": "phenom", "name": "Danaher", "slug": None, "url": "https://jobs.danaher.com"},
            {"ats": "greenhouse", "name": "ddome-wrong", "slug": "ddome", "url": "x"},
        ]
    )
)


@pytest.mark.parametrize(
    "ats,company,url,expected",
    [
        ("lever", "acme", "https://jobs.lever.co/acme/1234abcd-0000", "Acme Robotics"),
        (
            "phenom",
            "jobs.danaher.com",
            "https://jobs.danaher.com/global/en/job/R1/AI-Engineer",
            "Danaher",
        ),
        ("greenhouse", "ddome", GH.format(1), "DataDome"),
        ("icims", "steampunk", "https://careers-steampunk.icims.com/jobs/1/job", "steampunk"),
    ],
)
def test_the_company_name_comes_from_our_list_then_the_feeds_then_the_row(
    ats, company, url, expected
):
    names = {"greenhouse|ddome": "DataDome"}
    r = row(url=url, ats_type=ats, company=company)
    assert stapply.posting_from(r, "example_tertiary", names, FEED_NAMES)["company"] == expected


def test_fields_are_read_once_the_same_way(cfg, tmp_path, feed):
    lane, _ = read(cfg, tmp_path, feed)
    lv = next(r for r in lane.rows if "lever" in r["url"])
    assert lv["remote_flag"] is False
    assert (lv["salary_min"], lv["salary_max"]) == (166_400, 228_800)
    # Our parser reads Austin as the US; the feed's wrong "CA" is not used.
    assert lv["location_raw"] == "Austin, TX"


def test_the_country_code_fills_in_only_what_the_parser_cannot_read(cfg, tmp_path, feed):
    lane, _ = read(cfg, tmp_path, feed)
    eures = next(r for r in lane.rows if "eures" in r["url"])
    assert eures["location_raw"] == "DE (DE91C), Germany"
    assert stapply.located("", "SE") == "Sweden"
    assert stapply.located("Remote", "") == "Remote"


def test_boards_with_postings_in_the_file_are_covered(cfg, tmp_path, feed):
    _, crawl = read(cfg, tmp_path, feed)
    assert crawl.covered == {
        "greenhouse|ddome",
        "lever|acme",
        "workday|acme|wd5|external",
        "ashby|other",
    }


def _manifest(tmp_path, feed, hours_old=2, sha=None):
    when = datetime.now(UTC) - timedelta(hours=hours_old)
    digest = sha or hashlib.sha256(feed.read_bytes()).hexdigest()
    return {
        "generated_at": when.isoformat(),
        "all": {"parquet": "https://example.test/all.parquet", "parquet_sha256": digest},
        "companies": {"parquet": "https://example.test/companies.parquet"},
    }


def _wire(monkeypatch, manifest, feed):
    downloads = []
    monkeypatch.setattr(stapply.http_client, "fetch_json", lambda url, deadline_ts=None: manifest)

    def fake_download(url, dest, deadline_ts=None):
        downloads.append(url)
        if url.endswith("companies.parquet"):
            pd.DataFrame(
                [
                    {
                        "ats": "lever",
                        "name": "Acme Robotics",
                        "slug": "acme",
                        "url": "https://jobs.lever.co/acme",
                    }
                ]
            ).to_parquet(dest)
        else:
            shutil.copy(feed, dest)
        return dest.stat().st_size

    monkeypatch.setattr(stapply.http_client, "download", fake_download)
    return downloads


def test_a_stale_snapshot_is_skipped_and_said(monkeypatch, cfg, tmp_path, feed):
    downloads = _wire(monkeypatch, _manifest(tmp_path, feed, hours_old=50), feed)
    lane, crawl = stapply.StapplyLane(), make_crawl(cfg, tmp_path)
    lane.prepare(crawl)
    stats = lane.run(crawl)
    assert downloads == [] and crawl.covered == frozenset() and len(crawl.batch) == 0
    assert "50h old" in stats.errors[0] and "every board crawled" in stats.errors[0]


def test_a_checksum_mismatch_is_the_lanes_crash(monkeypatch, cfg, tmp_path, feed):
    _wire(monkeypatch, _manifest(tmp_path, feed, sha="0" * 64), feed)
    lane, crawl = stapply.StapplyLane(), make_crawl(cfg, tmp_path)
    lane.prepare(crawl)
    assert crawl.covered == frozenset()
    with pytest.raises(Exception, match="checksum"):
        lane.run(crawl)


def _page(desc):
    data = json.dumps({"@type": "JobPosting", "description": desc})
    return f"<script type='application/ld+json'>{data}</script>"


def test_run_saves_known_new_and_page_read_rows(monkeypatch, cfg, tmp_path, feed):
    _wire(monkeypatch, _manifest(tmp_path, feed), feed)
    monkeypatch.setattr(
        stapply.http_client, "fetch_page", lambda url: (200, _page("<p>" + JD + "</p>"))
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    known = {"greenhouse|1": {"updated_at": ""}}
    lane, crawl = stapply.StapplyLane(), make_crawl(cfg, tmp_path, known)
    lane.prepare(crawl)
    stats = lane.run(crawl)
    assert (stats.passed, stats.known, stats.new, stats.detail_ok, stats.short_jd) == (
        4,
        1,
        3,
        1,
        0,
    )
    assert "snapshot" in stats.notes[0]
    yc = next(r for r in crawl.batch.rows.values() if "ycombinator" in r["url"])
    assert yc["jd_text"].startswith("Build and ship")


def test_a_page_with_no_job_posting_block_leaves_the_row_out(monkeypatch, cfg, tmp_path, feed):
    _wire(monkeypatch, _manifest(tmp_path, feed), feed)
    monkeypatch.setattr(
        stapply.http_client, "fetch_page", lambda url: (200, "<html>app shell</html>")
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = stapply.StapplyLane(), make_crawl(cfg, tmp_path)
    lane.prepare(crawl)
    stats = lane.run(crawl)
    assert (stats.detail_failed, stats.short_jd) == (1, 1)
    assert not any("ycombinator" in r["url"] for r in crawl.batch.rows.values())


def test_a_known_row_without_a_jd_is_only_touched(monkeypatch, cfg, tmp_path, feed):
    _wire(monkeypatch, _manifest(tmp_path, feed), feed)
    key = "url|ycombinator.com/companies/clip/jobs/x-ai-engineer"
    lane, crawl = stapply.StapplyLane(), make_crawl(cfg, tmp_path, {key: {"updated_at": ""}})
    lane.prepare(crawl)
    lane.run(crawl)
    assert key in crawl.batch.touched and key not in crawl.batch.rows


def test_a_site_whose_pages_never_read_is_given_up_for_the_night(monkeypatch, cfg, tmp_path):
    rows = [
        row(
            url=f"https://dead.example/jobs/{i}",
            ats_type="moka",
            company=f"Co{i}",
            description="Short.",
        )
        for i in range(stapply.MAX_HOST_FAILURES + 5)
    ]
    rows += [
        row(
            url=f"https://good.example/jobs/{i}",
            ats_type="yc",
            company=f"Good{i}",
            description="Short.",
        )
        for i in range(3)
    ]
    path = tmp_path / "feed.parquet"
    pd.DataFrame(rows).to_parquet(path)
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, _page("<p>" + JD + "</p>") if "good" in url else "<html></html>"

    monkeypatch.setattr(stapply.http_client, "fetch_page", fetch)
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    stats = lane.run(crawl)
    assert sum("dead" in c for c in calls) == stapply.MAX_HOST_FAILURES
    assert sum("good" in c for c in calls) == 3 and stats.detail_ok == 3
    assert "dead.example" in stats.notes[-1]


def test_the_lane_names_companies_from_the_feeds_list(monkeypatch, cfg, tmp_path, feed):
    _wire(monkeypatch, _manifest(tmp_path, feed), feed)
    lane, crawl = stapply.StapplyLane(), make_crawl(cfg, tmp_path)
    lane.prepare(crawl)
    assert next(r for r in lane.rows if "lever" in r["url"])["company"] == "Acme Robotics"


def test_a_missing_company_list_costs_names_not_the_night(monkeypatch, cfg, tmp_path, feed):
    _wire(monkeypatch, _manifest(tmp_path, feed), feed)
    real = stapply.http_client.download

    def no_list(url, dest, deadline_ts=None):
        if url.endswith("companies.parquet"):
            raise stapply.CareersError("HTTP 503")
        return real(url, dest, deadline_ts)

    monkeypatch.setattr(stapply.http_client, "download", no_list)
    lane, crawl = stapply.StapplyLane(), make_crawl(cfg, tmp_path)
    lane.prepare(crawl)
    assert lane.failure is None and len(lane.rows) == 4
    assert next(r for r in lane.rows if "lever" in r["url"])["company"] == "acme"
    assert any("company list" in n for n in lane.stats.notes)


def _employer_page(name, desc=""):
    data = json.dumps(
        {
            "@type": "JobPosting",
            "description": desc,
            "hiringOrganization": {"@type": "Organization", "name": name},
        }
    )
    return f"<script type='application/ld+json'>{data}</script>"


def _one_row_feed(tmp_path, **kw):
    path = tmp_path / "one.parquet"
    pd.DataFrame([row(**kw)]).to_parquet(path)
    return path


def test_a_slug_company_is_named_from_its_page_once(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(
        tmp_path,
        url="https://careers-lennox.icims.com/jobs/1/job",
        ats_type="icims",
        company="lennox",
    )
    calls = []
    monkeypatch.setattr(
        stapply.http_client,
        "fetch_page",
        lambda url: calls.append(url) or (200, _employer_page("Lennox International")),
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    [saved] = crawl.batch.rows.values()
    assert (saved["company"], saved["company_normalized"]) == (
        "Lennox International",
        "lennox international",
    )
    assert len(calls) == 1 and "1 company names read from job pages" in lane.stats.notes


def test_one_page_read_gives_both_the_jd_and_the_name(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(
        tmp_path,
        url="https://careers-lennox.icims.com/jobs/1/job",
        ats_type="icims",
        company="lennox",
        description="Short.",
    )
    calls = []
    monkeypatch.setattr(
        stapply.http_client,
        "fetch_page",
        lambda url: (
            calls.append(url) or (200, _employer_page("Lennox International", "<p>" + JD + "</p>"))
        ),
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    [saved] = crawl.batch.rows.values()
    assert len(calls) == 1 and saved["company"] == "Lennox International"
    assert saved["jd_text"].startswith("Build and ship")


def test_a_known_row_keeps_its_better_stored_name(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(
        tmp_path,
        url="https://careers-lennox.icims.com/jobs/1/job",
        ats_type="icims",
        company="lennox",
    )
    monkeypatch.setattr(
        stapply.http_client, "fetch_page", lambda url: pytest.fail("no read needed")
    )
    key = S.make_posting(url="https://careers-lennox.icims.com/jobs/1/job")["posting_key"]
    lane, crawl = read(
        cfg, tmp_path, path, known={key: {"updated_at": "", "company": "Lennox International"}}
    )
    lane.run(crawl)
    assert crawl.batch.rows[key]["company"] == "Lennox International"


def test_a_page_with_no_employer_leaves_the_name(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(
        tmp_path,
        url="https://obyteshr.bamboohr.com/careers/37",
        ats_type="bamboohr",
        company="obyteshr",
    )
    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: (200, "<html></html>"))
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    [saved] = crawl.batch.rows.values()
    assert saved["company"] == "obyteshr"


def test_giving_up_counts_a_whole_site_not_one_subdomain(monkeypatch, cfg, tmp_path):
    rows = [
        row(url=f"https://co{i}.bamboohr.com/careers/{i}", ats_type="bamboohr", company=f"co{i}x")
        for i in range(stapply.MAX_HOST_FAILURES + 4)
    ]
    path = tmp_path / "many.parquet"
    pd.DataFrame(rows).to_parquet(path)
    calls = []
    monkeypatch.setattr(
        stapply.http_client, "fetch_page", lambda url: calls.append(url) or (200, "")
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    stats = lane.run(crawl)
    assert len(calls) == stapply.MAX_HOST_FAILURES and len(crawl.batch.rows) == len(rows)
    assert "bamboohr.com" in stats.notes[-1]


ORACLE = "https://jpmc.fa.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=CX_1001#{}"


def test_an_oracle_row_reads_its_jd_from_the_requisition_api(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(
        tmp_path,
        url=ORACLE.format(42),
        ats_type="oracle",
        company="JPMorgan Chase",
        description="Short.",
    )
    calls = []
    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: pytest.fail("no page read"))
    monkeypatch.setattr(
        stapply.http_client,
        "fetch_json",
        lambda url, deadline_ts=None: (
            calls.append(url)
            or {
                "items": [
                    {
                        "ExternalDescriptionStr": "<p>" + JD + "</p>",
                        "CorporateDescriptionStr": "<p>Blurb.</p>",
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    [saved] = crawl.batch.rows.values()
    assert "finder=ById;Id=%2242%22,siteNumber=CX_1001" in calls[0]
    assert saved["jd_text"].startswith("Build and ship") and "Blurb" not in saved["jd_text"]
    assert saved["posting_key"] == "oracle|jpmc|42"


def _site_page(label):
    return f'<head><meta property="og:site_name" content="{label}"/></head>'


def test_a_tenant_coded_oracle_company_is_named_from_its_site_once(monkeypatch, cfg, tmp_path):
    url = "https://egug.fa.us2.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=CX_1#{}"
    rows = [row(url=url.format(i), ats_type="oracle", company="Egug") for i in (1, 2)]
    path = tmp_path / "oracle.parquet"
    pd.DataFrame(rows).to_parquet(path)
    calls = []
    monkeypatch.setattr(
        stapply.http_client,
        "fetch_page",
        lambda url: calls.append(url) or (200, _site_page("American Express")),
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    assert calls == ["https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"]
    assert {r["company"] for r in crawl.batch.rows.values()} == {"American Express"}


@pytest.mark.parametrize(
    "label, expected",
    [
        ("American Express", "American Express"),
        ("Bank of England Job Board -", "Bank of England"),
        ("Chartwell Career Site English", "Chartwell"),
        ("UNIPOL_External Career Site", "UNIPOL"),
        ("Legrand Group Opportunities - Join us", "Legrand Group"),
        ("Airtel Africa Careers", "Airtel Africa"),
        ("Jobs onsemi", "onsemi"),
        ("Candidate Experience site", ""),
    ],
)
def test_a_site_label_is_cut_to_the_employer(label, expected):
    assert oracle.site_name(_site_page(label)) == expected


BA = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{}"


def _ba_rows(n):
    return [
        row(url=BA.format(f"10000-{i}-S"), ats_type="bundesagentur", company="Acme GmbH")
        for i in range(n)
    ]


def test_an_arbeitsagentur_row_reads_its_jd_from_the_detail_api(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(
        tmp_path,
        url=BA.format("13319-871150/1_622307LS-S"),
        ats_type="bundesagentur",
        company="Acme GmbH",
        description=None,
    )
    calls = []

    def fetch_json(url, deadline_ts=None, headers=None):
        calls.append((url, headers))
        return {"stellenangebotsBeschreibung": "## Aufgaben\n\n" + JD}

    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: pytest.fail("no page read"))
    monkeypatch.setattr(stapply.http_client, "fetch_json", fetch_json)
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    [saved] = crawl.batch.rows.values()
    # The whole tail, "/" included, is the reference number.
    assert calls == [
        (arbeitsagentur.API + "MTMzMTktODcxMTUwLzFfNjIyMzA3TFMtUw==", arbeitsagentur.HEADERS)
    ]
    assert saved["jd_text"].startswith("## Aufgaben\n\nBuild and ship")


def test_a_posting_the_api_no_longer_holds_falls_back_to_its_page(monkeypatch, cfg, tmp_path):
    n = stapply.MAX_HOST_FAILURES + 2
    path = tmp_path / "ba.parquet"
    pd.DataFrame(_ba_rows(n)).assign(description=None).to_parquet(path)
    api, pages = [], []

    def gone(url, deadline_ts=None, headers=None):
        api.append(url)
        raise stapply.CareersError("HTTP 404", status=404, permanent=True)

    monkeypatch.setattr(stapply.http_client, "fetch_json", gone)
    monkeypatch.setattr(
        stapply.http_client, "fetch_page", lambda url: pages.append(url) or (200, _page(JD))
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    stats = lane.run(crawl)
    # A 404 is that posting's answer: the API is never given up over it.
    assert len(api) == n and len(pages) == n and len(crawl.batch.rows) == n
    assert stats.errors == []


def test_a_short_api_jd_still_reads_the_page(monkeypatch, cfg, tmp_path):
    path = tmp_path / "ba.parquet"
    pd.DataFrame(_ba_rows(1)).assign(description=None).to_parquet(path)
    monkeypatch.setattr(
        stapply.http_client,
        "fetch_json",
        lambda url, deadline_ts=None, headers=None: {"stellenangebotsBeschreibung": "Siehe Link."},
    )
    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: (200, _page(JD)))
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    [saved] = crawl.batch.rows.values()
    assert saved["jd_text"].startswith("Build and ship")


def _night(cfg, tmp_path, path, day):
    crawl = B.Crawl(
        DiscoveryConfig(),
        cfg,
        TODAY + pd.Timedelta(days=day),
        0.0,
        {},
        S.Batch(),
        board_state=tmp_path / "b.parquet",
        boards_dir=tmp_path,
        failed_reads=tmp_path / "failed.json",
    )
    lane = stapply.StapplyLane()
    lane.read(path, crawl)
    return lane.run(crawl), crawl


def test_a_page_that_keeps_failing_is_skipped_then_retried(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(
        tmp_path, url="https://dead.example/jobs/1", company="Dead Co", description="Short."
    )
    reads = []
    monkeypatch.setattr(
        stapply.http_client, "fetch_page", lambda url: reads.append(url) or (200, "<html/>")
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    src = DiscoveryConfig().sources["stapply"]
    fails, retry = src.page_read_max_failures, src.page_read_retry_days
    for day in range(fails):
        _night(cfg, tmp_path, path, day)
    stats, _ = _night(cfg, tmp_path, path, fails)
    assert len(reads) == fails and "1 page reads skipped" in stats.notes[0]
    # A fixed page comes back on the retry.
    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: (200, _page(JD)))
    _, crawl = _night(cfg, tmp_path, path, fails - 1 + retry)
    assert len(crawl.batch.rows) == 1
    assert json.loads((tmp_path / "failed.json").read_text(encoding="utf-8")) == {}


def test_the_record_never_skips_an_api_read(monkeypatch, cfg, tmp_path):
    path = tmp_path / "ba.parquet"
    pd.DataFrame(_ba_rows(1)).assign(description=None).to_parquet(path)
    key = S.make_posting(url=BA.format("10000-0-S"))["posting_key"]
    (tmp_path / "failed.json").write_text(
        json.dumps({key: {"failures": 9, "last_try": str(TODAY.date())}}), encoding="utf-8"
    )
    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: pytest.fail("no page"))
    monkeypatch.setattr(
        stapply.http_client,
        "fetch_json",
        lambda url, deadline_ts=None, headers=None: {"stellenangebotsBeschreibung": JD},
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    _, crawl = _night(cfg, tmp_path, path, 1)
    assert [r["posting_key"] for r in crawl.batch.rows.values()] == [key]


def test_an_unreadable_record_starts_afresh(monkeypatch, cfg, tmp_path):
    path = _one_row_feed(tmp_path, company="DataDome")
    (tmp_path / "failed.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: pytest.fail("no page"))
    stats, crawl = _night(cfg, tmp_path, path, 0)
    assert "unreadable" in stats.notes[0] and len(crawl.batch.rows) == 1
    assert json.loads((tmp_path / "failed.json").read_text(encoding="utf-8")) == {}


UUID = "0000abcd-0000-0000-0000-000000000000"


@pytest.mark.parametrize(
    "url, module, name, payload, api",
    [
        (
            GH.format(7),
            greenhouse,
            "fetch_json",
            {"title": "Cog Engineer", "content": "&lt;p&gt;" + JD + "&lt;/p&gt;"},
            "https://boards-api.greenhouse.io/v1/boards/ddome/jobs/7",
        ),
        (
            f"https://jobs.lever.co/acme/{UUID}",
            lever,
            "fetch_json",
            {
                "text": "Cog Engineer",
                "hostedUrl": f"https://jobs.lever.co/acme/{UUID}",
                "descriptionPlain": JD,
            },
            f"https://api.lever.co/v0/postings/acme/{UUID}",
        ),
        (
            f"https://jobs.ashbyhq.com/acme/{UUID}",
            ashby,
            "fetch_json_post",
            {"data": {"jobPosting": {"id": UUID, "descriptionHtml": "<p>" + JD + "</p>"}}},
            ashby.GRAPHQL_URL + "?op=ApiJobPosting",
        ),
        (
            WD.format(9),
            stapply.http_client,
            "fetch_json",
            {"jobPostingInfo": {"jobDescription": "<p>" + JD + "</p>"}},
            "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Berlin/AI-Engineer_R9",
        ),
    ],
)
def test_a_board_hosted_row_reads_its_jd_from_the_board_api(
    monkeypatch, cfg, tmp_path, url, module, name, payload, api
):
    path = _one_row_feed(tmp_path, url=url, company="Acme", description="Short.")
    calls = []
    monkeypatch.setattr(stapply.http_client, "fetch_page", lambda url: pytest.fail("no page read"))
    monkeypatch.setattr(
        module, name, lambda url, *a, deadline_ts=None, **k: calls.append(url) or payload
    )
    monkeypatch.setattr(stapply.http_client.time, "sleep", lambda s: None)
    lane, crawl = read(cfg, tmp_path, path)
    lane.run(crawl)
    [saved] = crawl.batch.rows.values()
    assert calls == [api] and saved["jd_text"].startswith("Build and ship")


@pytest.mark.parametrize(
    "url, api",
    [
        ("https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/Berlin/AI_R1", "/Ext/job/Berlin/AI_R1"),
        ("https://acme.wd5.myworkdayjobs.com/us/job/Austin/AI_R1", "/us/job/Austin/AI_R1"),
        ("https://acme.wd5.myworkdayjobs.com/Ext", ""),
    ],
)
def test_a_workday_url_names_its_detail_api(url, api):
    base = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme"
    assert workday.detail_api_url(url) == (base + api if api else "")
