"""material/pick.py and discovery/liveness.py: which New roles the night tailors."""

from __future__ import annotations

import json

import pandas as pd

from applyourself.discovery import liveness
from applyourself.material import pick
from applyourself.shared import clock, http_client, paths
from tests.factories import write_clean, write_state

FULL = "Build retrieval pipelines and evals for a clinical trials platform. " * 30


def _world(rows, pairs=(), states=()):
    paths.CLEAN.parent.mkdir(parents=True, exist_ok=True)
    write_clean(paths.CLEAN, *rows)
    paths.MAYBE_DUPES.write_text(
        "".join(
            json.dumps({"pair": "|".join(sorted((a, b))), "job_id": a, "jd_score": s, "reason": r})
            + "\n"
            for a, b, s, r in pairs
        ),
        encoding="utf-8",
    )
    for jid, state in states:
        write_state(paths.PIPELINE, jid, state)


def test_one_role_per_duplicate_group_and_thin_jds_wait():
    _world(
        [
            {"job_id": "aaaa0001", "jd_text": FULL},
            {"job_id": "aaaa0002", "jd_text": FULL},
            {"job_id": "aaaa0003", "jd_text": FULL},
            {"job_id": "aaaa0004", "jd_text": FULL},
            {"job_id": "aaaa0005", "jd_text": "Short teaser."},
        ],
        pairs=[
            ("aaaa0001", "aaaa0002", 100, "same_board"),  # picked first tonight: 0002 waits
            ("aaaa0003", "aaaa0004", 92, "backfill_other_title"),  # below 95: both go
            ("aaaa0004", "bbbb0009", 99, "backfill_merge"),  # partner idle (saved): no stop
        ],
        states=[("bbbb0009", "saved")],
    )
    ids = ["aaaa0001", "aaaa0002", "aaaa0003", "aaaa0004", "aaaa0005"]
    picked, skipped = pick.pick(ids)
    assert picked == ["aaaa0001", "aaaa0003", "aaaa0004"]
    assert skipped == {
        "aaaa0002": {"why": "dupe", "detail": "duplicate of aaaa0001 (picked tonight)"},
        "aaaa0005": {"why": "thin", "detail": "JD 13 chars"},
    }


def test_an_old_posting_is_skipped_only_when_its_board_says_gone(monkeypatch):
    old = (pd.Timestamp(clock.today()) - pd.Timedelta(days=60)).normalize()
    _world(
        [
            {"job_id": "cccc0001", "jd_text": FULL, "posted_date": old, "url": "https://a.test/1"},
            {"job_id": "cccc0002", "jd_text": FULL, "posted_date": old, "url": "https://b.test/2"},
        ]
    )

    def page(url, timeout=0):
        if "a.test" in url:
            return 404, "Not Found"
        raise http_client.requests.ConnectionError("down")  # cannot tell: tailor as before

    monkeypatch.setattr(http_client, "fetch_page", page)
    monkeypatch.setattr(pick, "LIVE_CHECK_PACING", 0)
    picked, skipped = pick.pick(["cccc0001", "cccc0002"])
    assert picked == ["cccc0002"] and skipped["cccc0001"]["why"] == "gone"


def test_workday_says_gone_with_a_403_s22(monkeypatch):
    url = "https://acme.wd5.myworkdayjobs.com/acme_careers/job/Remote/AI-Engineer_R123"
    answers = iter([(403, '{"errorCode":"S22"}'), (403, '{"errorCode":"X1"}')])
    monkeypatch.setattr(http_client, "fetch_page", lambda u, timeout=0: next(answers))
    assert liveness.posting_status(url) == liveness.GONE
    assert liveness.posting_status(url) == liveness.UNKNOWN
    assert liveness.posting_status("https://www.linkedin.com/jobs/view/1") == liveness.UNKNOWN
