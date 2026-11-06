"""The apply-URL resolver: stored data only, and one opt-in board lookup."""

from __future__ import annotations

from applyourself.apply import apply_url, sheet
from applyourself.discovery.sources.boards.base import Board
from applyourself.shared import paths
from tests.factories import scored_rows, write_clean, write_state

LI = "https://www.linkedin.com/jobs/view/4486872788"
WD = "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Pune/AI-Engineer_R123"
GH = "https://job-boards.greenhouse.io/acme/jobs/4012345"


def test_an_aggregator_listing_is_no_url_and_a_workday_posting_is_manual():
    assert apply_url.resolve([LI]).kind == apply_url.NO_URL
    assert apply_url.resolve([LI]).reason.startswith("linkedin listing")
    manual = apply_url.resolve([LI, WD])
    assert (manual.kind, manual.url) == (apply_url.MANUAL, WD) and "workday" in manual.reason


def test_a_board_link_in_the_jd_or_a_stored_lookup_makes_it_submittable():
    jd = f"Apply on our site ({GH}). Thanks."
    assert apply_url.resolve([LI], jd) == apply_url.Resolved(
        GH, apply_url.SUBMITTABLE, "link in the JD"
    )
    miss = {"apply_url": "", "via": "greenhouse board acme", "checked": "2026-11-01"}
    assert "not on greenhouse board acme" in apply_url.resolve([LI], "", miss).reason


def test_the_queue_shows_the_route_and_a_stored_url_reaches_resolve_url(tmp_path):
    import yaml

    from applyourself import apply_cli

    (tmp_path / "profile").mkdir()
    paths.JOBS.mkdir(exist_ok=True)
    (tmp_path / "profile" / "scoring.yaml").write_text(
        yaml.safe_dump({"cutoffs": {"review": 50, "tailor": 60, "keep_long": 70, "sheet": 80}}),
        encoding="utf-8",
    )
    scored_rows({"job_id": "aaaa1111", "fit_score": 90}).to_parquet(paths.SCORED)
    write_clean(
        paths.CLEAN,
        {
            "job_id": "aaaa1111",
            "company": "Acme",
            "title": "AI Engineer",
            "source": "linkedin",
            "url": LI,
        },
    )
    write_state(paths.PIPELINE, "aaaa1111", "tailored", url=LI, tailored_dirs=["lane/x"])
    row = sheet.queue()[0]
    assert (row["status"], row["note"]) == ("No apply URL", apply_url.resolve([LI]).reason)

    calls = []
    board = Board("greenhouse", "Acme", "acme")

    def fetch(b):
        calls.append(b.key)
        return [
            {"title": "Senior AI Engineer", "url": GH + "0"},
            {"title": "AI Engineer", "url": GH},
        ]

    found, errors = apply_url.find_on_boards(
        [{"job_id": "aaaa1111", "company": "Acme", "title": "AI Engineer"}],
        apply_url.boards_by_company([board]),
        fetch=fetch,
        pause=0,
    )
    assert errors == [] and calls == ["greenhouse|acme"] and found["aaaa1111"]["apply_url"] == GH
    apply_url.save(found)
    assert apply_cli.resolve_url("aaaa1111", {"url": LI}) == GH
    assert sheet.queue()[0]["status"] == "Resume ready"


def test_a_loose_title_match_takes_only_one_posting_and_never_another_level():
    def found(postings, title="Agent Engineer (Bengaluru)"):
        rows = [{"job_id": "j", "company": "Acme", "title": title}]
        board = apply_url.boards_by_company([Board("greenhouse", "Acme", "acme")])
        return apply_url.find_on_boards(rows, board, fetch=lambda b: postings, pause=0)[0]["j"]

    here = {"title": "Agent Engineer", "url": GH, "location_raw": "Bengaluru, India"}
    senior = {"title": "Senior Agent Engineer", "url": GH + "1", "location_raw": "Bengaluru"}
    assert found([here, senior])["apply_url"] == GH
    assert found([senior])["apply_url"] == ""
    assert found([here, {**here, "url": GH + "2"}])["apply_url"] == ""


def test_ai_spelled_out_and_an_own_company_tail_match_but_discovery_stays_strict():
    from applyourself.discovery.dedupe import titles_differ

    def found(postings, title="Artificial Intelligence Engineer at Acme Inc"):
        rows = [{"job_id": "j", "company": "Acme", "title": title}]
        board = apply_url.boards_by_company([Board("greenhouse", "Acme", "acme")])
        return apply_url.find_on_boards(rows, board, fetch=lambda b: postings, pause=0)[0]["j"]

    assert found([{"title": "AI Engineer", "url": GH}])["apply_url"] == GH
    assert found([{"title": "AI Engineer II", "url": GH}])["apply_url"] == ""
    assert found([{"title": "AI Engineer", "url": GH}], "AI Engineer at Globex")["apply_url"] == ""
    a, b = (
        {"title_normalized": "artificial intelligence engineer"},
        {"title_normalized": "ai engineer"},
    )
    assert titles_differ(a, b) and not titles_differ(a, b, loose=True)


def test_an_older_matchers_miss_is_looked_up_again_and_a_hit_never_is():
    assert apply_url.needs_lookup({"apply_url": "", "via": "greenhouse board acme"})
    assert not apply_url.needs_lookup({"apply_url": "", "matcher": apply_url.MATCHER})
    assert not apply_url.needs_lookup({"apply_url": GH})
