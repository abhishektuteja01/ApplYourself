"""run.py end to end with fake lanes, the search lane with jobspy faked, the
inbox, the digest, and ingest-url. No network."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.discovery import digest, ingest_url, live_view
from src.discovery import run as R
from src.discovery import store as S
from src.discovery.config import DiscoveryConfig
from src.discovery.sources import inbox as I
from src.discovery.sources import search as Q
from src.discovery.sources.boards import base as B
from src.discovery.sources.lane import Lane
from src.shared.location.allowlist import Allowed

TODAY = pd.Timestamp("2026-09-27")
JD = "Build and ship widget reconciliation systems for finance teams every day. " * 5


class Crawl(B.Crawl):
    def __init__(self, cfg, known=None):
        super().__init__(DiscoveryConfig(), cfg, TODAY, 0.0, known or {}, S.Batch())


# ---------- search lane ----------


def test_search_gates_touches_known_and_fetches_new_details(monkeypatch, cfg):
    known_url = "https://www.linkedin.com/jobs/view/1"
    frame = pd.DataFrame(
        [
            {
                "job_url": known_url,
                "title": "Widget Functional Consultant",
                "company": "Acme",
                "location": "Austin, TX",
                "description": None,
            },
            {
                "job_url": "https://www.linkedin.com/jobs/view/2",
                "title": "Widget Platform Analyst",
                "company": "Beta",
                "location": "",
                "description": None,
            },
            {
                "job_url": "https://www.linkedin.com/jobs/view/3",
                "title": "Senior Widget Analyst",
                "company": "Gamma",
                "location": "",
                "description": None,
            },
        ]
    )
    monkeypatch.setattr(Q, "scrape_jobs", lambda **kw: frame)
    monkeypatch.setattr(Q.time, "sleep", lambda s: None)

    class Client:
        def _get_job_details(self, job_id):
            return {"description": JD}

    monkeypatch.setattr(Q, "_detail_client", lambda tally: Client())
    crawl = Crawl(cfg, known={"linkedin|1": {}})
    crawl.cfg.search_locations = ["United States"]
    crawl.cfg.sources["linkedin"].remote_queries = False
    stats = Q.SearchLane("linkedin").run(crawl, tally=None)
    assert stats.known >= 1 and "linkedin|1" in crawl.batch.touched
    assert "linkedin|2" in crawl.batch.rows and "linkedin|3" not in crawl.batch.rows
    assert crawl.batch.rows["linkedin|2"]["jd_text"] == JD
    assert stats.detail_ok == 1


def test_search_names_each_query_that_hit_the_cap(monkeypatch, cfg):
    def rows(n, start):
        return pd.DataFrame(
            [
                {
                    "job_url": f"https://www.linkedin.com/jobs/view/{start + i}",
                    "title": "Widget Platform Analyst",
                    "company": f"Co{start + i}",
                    "location": "",
                    "description": JD,
                }
                for i in range(n)
            ]
        )

    frames = iter([rows(2, 10), rows(1, 20)])
    monkeypatch.setattr(Q, "scrape_jobs", lambda **kw: next(frames))
    monkeypatch.setattr(Q.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        Q,
        "build_queries",
        lambda *a: [
            ("v", "widget analyst", "United States", False),
            ("v", "widget platform", "United States", True),
        ],
    )
    monkeypatch.setattr(Q, "_detail_client", lambda tally: None)
    crawl = Crawl(cfg, known={"linkedin|11": {}})
    crawl.cfg.sources["linkedin"].results_wanted = 2
    stats = Q.SearchLane("linkedin").run(crawl, tally=None)
    assert stats.capped == ["widget analyst @ United States: 1 new"]
    assert "1 hit the 2-result cap" in stats.notes[0]


def test_build_queries_adds_abroad_after_home(cfg):
    src = DiscoveryConfig().sources["linkedin"]
    src.remote_queries = False
    queries = Q.build_queries("linkedin", src, cfg, ["United States"])
    assert all(q[2] == "United States" and q[3] is False for q in queries)


def test_build_queries_adds_company_queries_last(cfg):
    src = DiscoveryConfig().sources["linkedin"]
    src.remote_queries = False
    src.company_terms, src.company_ids = ["widget", "gizmo"], {"Acme": 123}
    queries = Q.build_queries("linkedin", src, cfg, ["United States"])
    assert queries[-2:] == [
        ("", "widget", "United States", False, [123]),
        ("", "gizmo", "United States", False, [123]),
    ]
    assert all(q[4] is None for q in queries[:-2])


def test_company_query_filters_by_id_and_classifies_by_title(monkeypatch, cfg):
    frame = pd.DataFrame(
        [
            {
                "job_url": "https://www.linkedin.com/jobs/view/5",
                "title": "Widget Functional Consultant",
                "company": "Acme",
                "location": "Austin, TX",
                "description": JD,
            },
            {
                "job_url": "https://www.linkedin.com/jobs/view/6",
                "title": "Office Chef",
                "company": "Acme",
                "location": "Austin, TX",
                "description": JD,
            },
        ]
    )
    sent = []
    monkeypatch.setattr(Q, "scrape_jobs", lambda **kw: sent.append(kw) or frame)
    monkeypatch.setattr(Q.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        Q, "build_queries", lambda *a: [("", "widget", "United States", False, [123])]
    )
    monkeypatch.setattr(Q, "_detail_client", lambda tally: None)
    crawl = Crawl(cfg)
    Q.SearchLane("linkedin").run(crawl, tally=None)
    assert sent[0]["linkedin_company_ids"] == [123]
    assert list(crawl.batch.rows) == ["linkedin|5"]
    assert crawl.batch.rows["linkedin|5"]["vertical"]


# ---------- inbox ----------


def test_inbox_moves_clips_only_after_done(tmp_path, cfg):
    (tmp_path / "a.md").write_text(
        "---\ncompany: Acme\ntitle: Widget Analyst\nurl: https://x.test/1\n---\n" + JD,
        encoding="utf-8",
    )
    (tmp_path / "bad.md").write_text("no frontmatter", encoding="utf-8")
    lane = I.InboxLane(tmp_path)
    crawl = Crawl(cfg)
    stats = lane.run(crawl)
    assert stats.passed == 1 and (tmp_path / "a.md").exists()
    assert (tmp_path / ".malformed" / "bad.md").exists()
    lane.done()
    assert not (tmp_path / "a.md").exists() and (tmp_path / ".processed" / "a.md").exists()
    row = next(iter(crawl.batch.rows.values()))
    assert row["source"] == "manual" and row["jd_text"].startswith("Build")


def test_inbox_never_overwrites_a_processed_clip(tmp_path, cfg):
    (tmp_path / ".processed").mkdir()
    (tmp_path / ".processed" / "a.md").write_text("old", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ncompany: A\ntitle: T\nurl: https://x.test/2\n---\n" + JD, encoding="utf-8"
    )
    lane = I.InboxLane(tmp_path)
    lane.run(Crawl(cfg))
    lane.done()
    assert (tmp_path / ".processed" / "a.md").read_text(encoding="utf-8") == "old"
    assert (tmp_path / ".processed" / "a.1.md").exists()


# ---------- run.main ----------


class FakeLane(B.BoardLane):
    name = "greenhouse"

    def run(self, crawl, tally=None):
        crawl.batch.upsert(
            S.make_posting(
                source="greenhouse",
                company="Acme",
                title="Widget Functional Consultant",
                url="https://job-boards.greenhouse.io/acme/jobs/1",
                jd_text=JD,
                vertical="example_primary",
                location_raw="Austin, TX",
            )
        )
        crawl.record_board(B.BoardResult(B.Board("greenhouse", "Acme", "acme"), "ok", 1, 1, 1))
        stats = B.LaneStats(boards_due=1, boards_checked=1, boards_ok=1, listed=1, passed=1, new=1)
        return stats


class Crashing(B.BoardLane):
    name = "lever"

    def run(self, crawl, tally=None):
        raise RuntimeError("boom")


class FakeFeed(Lane):
    name = "stapply"

    def prepare(self, crawl):
        crawl.covered = frozenset({"greenhouse|acme"})

    def run(self, crawl, tally=None):
        return B.LaneStats(listed=9, passed=0, notes=["snapshot x"])


class SeesCovered(FakeLane):
    def run(self, crawl, tally=None):
        assert crawl.covered == {"greenhouse|acme"}, "the feed prepares before board lanes start"
        return super().run(crawl)


class AllowAll:
    def __init__(self, cfg):
        pass

    def check(self, raw, vertical="", title=""):
        return Allowed(True, raw or "", bool(raw))


@pytest.fixture
def wired(monkeypatch, tmp_path):
    from src.shared.location import allowlist, parser

    monkeypatch.setattr(parser, "require_libpostal", lambda: None)
    monkeypatch.setattr(allowlist, "Allowlist", AllowAll)
    cfg = DiscoveryConfig()
    monkeypatch.setattr(R, "load_config", lambda: cfg)
    monkeypatch.setattr(
        R, "_lane", lambda name: {"greenhouse": FakeLane(), "lever": Crashing()}[name]
    )
    monkeypatch.setattr(R, "load_state_index", lambda p: {})
    return tmp_path


def test_main_saves_postings_writes_clean_and_contains_a_crash(wired):
    code = R.main(
        [
            "--jobs-dir",
            str(wired),
            "--source",
            "greenhouse",
            "--source",
            "lever",
            "--boards-dir",
            str(wired),
        ]
    )
    assert code == 0
    clean = pd.read_parquet(wired / "clean.parquet")
    assert list(clean.columns) == live_view.CLEAN_COLUMNS and len(clean) == 1
    [report] = [json.loads(f.read_text(encoding="utf-8")) for f in (wired / "runs").glob("*.json")]
    assert report["lanes"]["lever"]["status"] == "crashed"
    assert report["lanes"]["greenhouse"]["status"] == "ok"
    assert report["new_job_ids"] == list(clean["job_id"])
    assert report["view"]["clean"] == 1
    assert B.load_state(wired / "boards.parquet").at[0, "status"] == "ok"
    assert (wired / "runs" / f"{report['run_id']}.md").exists()


def test_the_feed_prepares_before_the_board_lanes(wired, monkeypatch):
    monkeypatch.setattr(
        R, "_lane", lambda name: {"greenhouse": SeesCovered(), "stapply": FakeFeed()}[name]
    )
    assert (
        R.main(
            [
                "--jobs-dir",
                str(wired),
                "--source",
                "stapply",
                "--source",
                "greenhouse",
                "--boards-dir",
                str(wired),
            ]
        )
        == 0
    )
    [report] = [json.loads(f.read_text(encoding="utf-8")) for f in (wired / "runs").glob("*.json")]
    assert report["lanes"]["greenhouse"]["status"] == "ok"
    assert report["lanes"]["stapply"]["notes"] == ["snapshot x"]
    md = (wired / "runs" / f"{report['run_id']}.md").read_text(encoding="utf-8")
    assert "| stapply | 0 |" not in md, "the feed has no row in the boards table"


def test_deadline_zero_only_rebuilds_the_view(wired):
    assert (
        R.main(["--jobs-dir", str(wired), "--deadline-hours", "0", "--boards-dir", str(wired)]) == 0
    )
    [report] = [json.loads(f.read_text(encoding="utf-8")) for f in (wired / "runs").glob("*.json")]
    assert report["lanes"] == {}


def test_unknown_source_is_refused(wired):
    with pytest.raises(SystemExit):
        R.main(["--jobs-dir", str(wired), "--source", "monster"])


def test_raw_debug_shards_are_pruned_by_date(tmp_path):
    p = R.Paths(jobs=tmp_path, boards_dir=tmp_path)
    (tmp_path / "raw").mkdir()
    old = tmp_path / "raw" / "2026-09-01_1630.parquet"
    pd.DataFrame({"x": [1]}).to_parquet(old)
    pruned = R._write_raw(
        [{"source": "lever", "title": "T", "vertical": "v", "passed": True, "posting_key": "k"}],
        p,
        "2026-09-27_1630",
        TODAY,
        3,
    )
    assert (
        pruned == 1 and not old.exists() and (tmp_path / "raw" / "2026-09-27_1630.parquet").exists()
    )


# ---------- digest ----------


def _report(run_id, passed, status="ok", errors=()):
    return {
        "run_id": run_id,
        "wall_time_s": 10,
        "new_job_ids": ["a"],
        "view": {"clean": 5},
        "lanes": {
            "lever": {
                "status": status,
                "passed": passed,
                "errors": list(errors),
                "http": {"403": 0, "429": 2},
                "boards_due": 4,
                "boards_checked": 4,
            }
        },
    }


def test_digest_trends(tmp_path):
    for i, n in enumerate([10, 12, 11, 9]):
        (tmp_path / f"2026-09-2{i}_1630.json").write_text(
            json.dumps(_report(f"2026-09-2{i}_1630", n)), encoding="utf-8"
        )
    (tmp_path / "2026-09-27_1630.json").write_text(
        json.dumps(_report("2026-09-27_1630", 0, "zero")), encoding="utf-8"
    )
    d = digest.build_digest(digest.load_reports(tmp_path))
    assert d["run_id"] == "2026-09-27_1630" and d["final_rows"] == 5 and d["new_job_ids"] == 1
    [t] = d["trends"]
    assert t["median_rows"] == 10.5 and t["silent_zero"] and t["http_429"] == 2
    assert d["silent_zeros"] == ["lever"]


def test_digest_cli_json(tmp_path, capsys):
    (tmp_path / "2026-09-27_1630.json").write_text(
        json.dumps(_report("2026-09-27_1630", 3)), encoding="utf-8"
    )
    assert digest.main(["--runs-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "2026-09-27_1630"


# ---------- ingest-url ----------


def test_ingest_refuses_before_the_store_exists(tmp_path):
    with pytest.raises(ingest_url.IngestError, match="discover --deadline-hours 0"):
        ingest_url.ingest("https://job-boards.greenhouse.io/acme/jobs/1", jobs_dir=tmp_path)


def test_ingest_a_generic_url_needs_company_and_title():
    with pytest.raises(ingest_url.IngestError, match="never guessed"):
        ingest_url.fetch_row("https://careers.example.com/job/1")


def test_ingest_saves_and_returns_the_id(monkeypatch, tmp_path, cfg):
    from src.shared.location import allowlist

    monkeypatch.setattr(allowlist, "Allowlist", AllowAll)
    monkeypatch.setattr(ingest_url, "load_config", lambda: DiscoveryConfig())
    monkeypatch.setattr(R, "load_state_index", lambda p: {})
    monkeypatch.setattr(ingest_url, "load_state_index", lambda p: {})
    monkeypatch.setattr(ingest_url, "fetch_page_text", lambda url: JD)
    S.save_store(S.empty_store(), tmp_path / "jobs.parquet")
    info = ingest_url.ingest(
        "https://careers.example.com/job/1",
        vertical="example_primary",
        company="Acme",
        title="Widget Analyst",
        jobs_dir=tmp_path,
    )
    assert info["source"] == "manual" and len(info["job_id"]) == 8 and not info["attached"]
    again = ingest_url.ingest(
        "https://careers.example.com/job/1?src=LinkedIn",
        vertical="example_primary",
        company="Acme",
        title="Widget Analyst",
        jobs_dir=tmp_path,
    )
    assert again["job_id"] == info["job_id"] and again["attached"]


def test_ingest_reads_a_pages_job_posting_block_first(monkeypatch):
    from src.discovery import ingest_url

    page = (
        "<nav>Jobs Companies Log in</nav><script type='application/ld+json'>"
        '{"@type": "JobPosting", "description": "<p>Own the eval harness.</p>"}</script>'
    )
    monkeypatch.setattr(ingest_url.http, "fetch_page", lambda url: (200, page))
    assert (
        ingest_url.fetch_page_text("https://www.ycombinator.com/companies/x/jobs/y")
        == "Own the eval harness."
    )
    monkeypatch.setattr(ingest_url.http, "fetch_page", lambda url: (200, "<p>Plain page JD.</p>"))
    assert ingest_url.fetch_page_text("https://example.com/job") == "Plain page JD."
