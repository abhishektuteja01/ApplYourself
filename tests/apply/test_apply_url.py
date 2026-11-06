"""The apply-URL resolver: stored data only, and one opt-in board lookup."""

from __future__ import annotations

from src.apply import apply_url, sheet
from src.discovery.sources.boards.base import Board
from src.shared import paths
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

    from src import apply_cli

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
