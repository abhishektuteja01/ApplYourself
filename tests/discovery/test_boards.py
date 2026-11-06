"""sources/boards.py: the board list, the due rule, board state, and the
one board loop, with a fake lane and no network."""

from __future__ import annotations

import time

import pandas as pd
import pytest

from applyourself.discovery import store as S
from applyourself.discovery.config import DiscoveryConfig
from applyourself.discovery.sources.boards import base as B
from applyourself.shared.http_client import CareersError
from applyourself.shared.job_boards import board_key

TODAY = pd.Timestamp("2026-09-27")
JD = "Build retrieval systems for clinical data and ship them to production daily. " * 5


def write(path, rows):
    path.write_text(
        "ats,name,slug,active\n" + "\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8"
    )


# ---------- the board list ----------


def test_case_duplicates_fold_and_the_last_row_wins_the_name(tmp_path):
    write(
        tmp_path / "boards.csv",
        [("greenhouse", "Acme", "acme", ""), ("greenhouse", "Acme Inc", "ACME", "")],
    )
    boards = B.load_boards(boards_dir=tmp_path)
    assert [(b.name, b.slug) for b in boards] == [("Acme Inc", "acme")]


def test_a_retired_spelling_does_not_retire_its_live_twin(tmp_path):
    write(tmp_path / "boards.csv", [("lever", "2a", "2A", ""), ("lever", "2a", "2a", "0")])
    assert [b.slug for b in B.load_boards(boards_dir=tmp_path)] == ["2A"]


def test_a_tombstone_outlives_a_readd(tmp_path):
    write(
        tmp_path / "boards.csv",
        [("greenhouse", "Dead", "dead", "0"), ("greenhouse", "Dead", "dead", "")],
    )
    assert B.load_boards(boards_dir=tmp_path) == []
    assert len(B.load_boards(boards_dir=tmp_path, include_retired=True)) == 1


def test_the_retired_list_outlives_a_readd_and_keeps_the_hand_name(tmp_path):
    (tmp_path / "boards.csv").write_text(
        "ats,name,slug\nlever,Fliff,Fliff\nlever,Gone,gone\n", encoding="utf-8"
    )
    (tmp_path / "boards_retired.csv").write_text(
        "ats,name,slug\nlever,fliff old,fliff\nlever,Gone,gone\n", encoding="utf-8"
    )
    assert [(b.name, b.slug) for b in B.load_boards(boards_dir=tmp_path)] == [("Fliff", "Fliff")]
    assert len(B.load_boards(boards_dir=tmp_path, include_retired=True)) == 2


def test_unknown_ats_and_blank_rows_are_skipped(tmp_path):
    write(tmp_path / "boards.csv", [("taleo", "X", "x", ""), ("lever", "", "y", "")])
    assert B.load_boards(boards_dir=tmp_path) == []


# ---------- due rule and state ----------


def _state(rows):
    return pd.DataFrame([dict(zip(B.STATE_COLUMNS, r)) for r in rows], columns=B.STATE_COLUMNS)


def test_due_boards_order_and_every_days():
    boards = [B.Board("workday", n, n) for n in ("a", "b", "c", "d")]
    state = _state(
        [
            ("workday", "a", "A", TODAY - pd.Timedelta(days=1), pd.NaT, "ok", 0, 0, 0, 0),
            ("workday", "b", "B", TODAY - pd.Timedelta(days=5), pd.NaT, "ok", 0, 0, 0, 0),
            ("workday", "c", "C", TODAY - pd.Timedelta(days=3), pd.NaT, "ok", 0, 0, 0, 0),
        ]
    )
    due = B.due_boards(boards, state, TODAY, every_days=3, dead_every_days=14)
    assert [b.slug for b in due] == ["d", "b", "c"]


def test_dead_boards_wait_longer():
    board = B.Board("lever", "x", "x")
    state = _state([("lever", "x", "x", TODAY - pd.Timedelta(days=5), pd.NaT, "dead", 0, 0, 0, 3)])
    assert B.due_boards([board], state, TODAY, 1, 14) == []
    assert B.due_boards([board], state, TODAY, 1, 5) == [board]


def test_update_state_strikes_errors_and_found(tmp_path):
    path = tmp_path / "boards.parquet"
    b = B.Board("lever", "X", "x")
    for _ in range(3):
        B.update_state([B.BoardResult(b, "404")], TODAY, path=path)
    assert B.load_state(path).at[0, "status"] == "dead"
    B.update_state([B.BoardResult(b, "ok", listed=5, passed=2, new=1)], TODAY, path=path)
    row = B.load_state(path).iloc[0]
    assert row["status"] == "ok" and row["strikes"] == 0 and row["last_found"] == TODAY
    B.update_state([B.BoardResult(b, "error")], TODAY + pd.Timedelta(days=1), path=path)
    row = B.load_state(path).iloc[0]
    assert row["status"] == "error" and row["last_checked"] == TODAY


# ---------- the loop ----------


class FakeLane(B.BoardLane):
    name = "greenhouse"

    def __init__(self, listings, detail=None, fail=()):
        self.listings, self._detail, self.fail = listings, detail, set(fail)
        self.detail_calls = 0

    def list_board(self, board, crawl, pause):
        if board.slug in self.fail:
            raise CareersError("gone", status=404, permanent=True)
        return [dict(p) for p in self.listings.get(board.slug, [])]

    def detail(self, board, posting, crawl):
        self.detail_calls += 1
        return None if self._detail is None else {**posting, "jd_text": self._detail}


def crawl_for(tmp_path, slugs, known=None, cfg=None):
    write(tmp_path / "boards.csv", [("greenhouse", s.title(), s, "") for s in slugs])
    cfg = cfg or DiscoveryConfig()
    cfg.sources["greenhouse"].pacing_seconds = 0.5
    return B.Crawl(
        cfg,
        None,
        TODAY,
        0.0,
        known or {},
        S.Batch(),
        board_state=tmp_path / "boards.parquet",
        boards_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda s: None)


def post(slug, n, title="Widget Functional Consultant", jd=JD):
    return S.make_posting(
        source="greenhouse",
        company=slug.title(),
        title=title,
        url=f"https://job-boards.greenhouse.io/{slug}/jobs/{n}",
        jd_text=jd,
    )


def test_loop_gates_titles_and_saves_new_postings(tmp_path, cfg):
    lane = FakeLane({"acme": [post("acme", 1), post("acme", 2, title="Warehouse Associate")]})
    crawl = crawl_for(tmp_path, ["acme"])
    crawl.vcfg = cfg
    stats = lane.run(crawl)
    assert (stats.listed, stats.passed, stats.new) == (2, 1, 1)
    assert list(crawl.batch.rows) == [post("acme", 1)["posting_key"]]
    [result] = crawl.take_board_results()
    assert (result.status, result.listed, result.passed, result.new) == ("ok", 2, 1, 1)


def test_known_posting_with_a_jd_refreshes_without_a_detail_call(tmp_path, cfg):
    p = post("acme", 1)
    lane = FakeLane({"acme": [p]})
    crawl = crawl_for(tmp_path, ["acme"], known={p["posting_key"]: {"updated_at": ""}})
    crawl.vcfg = cfg
    stats = lane.run(crawl)
    assert stats.known == 1 and stats.new == 0 and lane.detail_calls == 0
    assert p["posting_key"] in crawl.batch.rows


def test_new_posting_without_a_jd_is_fetched_once(tmp_path, cfg):
    lane = FakeLane({"acme": [post("acme", 1, jd="")]}, detail=JD)
    crawl = crawl_for(tmp_path, ["acme"])
    crawl.vcfg = cfg
    stats = lane.run(crawl)
    assert lane.detail_calls == 1 and stats.detail_ok == 1 and len(crawl.batch.rows) == 1


def test_a_short_jd_is_not_stored(tmp_path, cfg):
    lane = FakeLane({"acme": [post("acme", 1, jd="")]}, detail="too short")
    crawl = crawl_for(tmp_path, ["acme"])
    crawl.vcfg = cfg
    stats = lane.run(crawl)
    assert stats.short_jd == 1 and not crawl.batch.rows


def test_404_board_is_recorded_and_the_rest_continue(tmp_path, cfg):
    lane = FakeLane({"beta": [post("beta", 1)]}, fail={"acme"})
    crawl = crawl_for(tmp_path, ["acme", "beta"])
    crawl.vcfg = cfg
    stats = lane.run(crawl)
    assert stats.boards_404 == 1 and stats.boards_ok == 1 and len(crawl.batch.rows) == 1


def test_deadline_stops_the_loop(tmp_path, cfg):
    lane = FakeLane({"acme": [post("acme", 1)]})
    crawl = crawl_for(tmp_path, ["acme"])
    crawl.vcfg = cfg
    crawl.deadline_ts = time.time() - 1
    stats = lane.run(crawl)
    assert stats.truncated and stats.boards_checked == 0


def test_a_broken_parser_fails_the_lane(tmp_path, cfg):
    class Broken(FakeLane):
        def list_board(self, board, crawl, pause):
            raise KeyError("jobs")

    crawl = crawl_for(tmp_path, [f"b{i:02d}" for i in range(12)])
    crawl.vcfg = cfg
    with pytest.raises(RuntimeError, match="API shape"):
        Broken({}).run(crawl)


def test_a_board_the_feed_covered_is_not_crawled_and_stays_due(tmp_path, cfg):
    lane = FakeLane({"acme": [post("acme", 1)], "beta": [post("beta", 1)]})
    crawl = crawl_for(tmp_path, ["acme", "beta"])
    crawl.vcfg, crawl.covered = cfg, frozenset({"greenhouse|acme"})
    stats = lane.run(crawl)
    assert [r.board.slug for r in crawl.take_board_results()] == ["beta"]
    assert stats.boards_due == 1 and stats.notes == ["1 due boards left to the feed"]


@pytest.mark.parametrize(
    "url,key",
    [
        ("https://job-boards.greenhouse.io/DataDome/jobs/1", "greenhouse|datadome"),
        (
            "https://job-boards.anz.greenhouse.io/futuresecureai/jobs/4001528201",
            "greenhouse|futuresecureai",
        ),
        ("https://jobs.lever.co/acme/1234abcd-0000", "lever|acme"),
        (
            "https://acme.wd5.myworkdayjobs.com/External/job/Berlin/AI_R1",
            "workday|acme|wd5|external",
        ),
        ("https://www.linkedin.com/jobs/view/1", ""),
    ],
)
def test_board_key_matches_the_board_list_key(url, key):
    assert board_key(url) == key
    if key.startswith("workday"):
        assert B.Board("workday", "Acme", "acme|wd5|External").key == key
        # The store's board stays the tenant alone.
        assert S.make_posting(url=url, title="t", company="c")["board"] == "workday|acme"
