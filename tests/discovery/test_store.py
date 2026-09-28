"""store.py: job_id rules, refresh/touch, and the live view."""
from __future__ import annotations

import pandas as pd
import pytest

from src.discovery import normalize as N
from src.discovery import store as S
from src.discovery.location import Allowed

TODAY = pd.Timestamp("2026-09-27")
JD = "Build retrieval pipelines for a clinical trials platform and ship them. " * 10
JD_OTHER = "Run the warehouse shift schedule and own inventory accuracy end to end. " * 10


def post(url, company="Acme", title="AI Engineer", source="greenhouse", jd=JD, **kw):
    return S.make_posting(url=url, company=company, title=title, source=source,
                          jd_text=jd, vertical=kw.pop("vertical", "primary"), **kw)


def fold(store, *rows, touched=(), tracked=frozenset(), today=TODAY, live_days=14):
    batch = S.Batch()
    for r in rows:
        batch.upsert(r)
    for k in touched:
        batch.touch(k)
    return S.apply_batch(store, batch, today, tracked, live_days)


GH = "https://job-boards.greenhouse.io/acme/jobs/{}"


# ---------- id rules ----------

def test_new_posting_gets_the_hash():
    store, stats = fold(S.empty_store(), post(GH.format(1)))
    assert store.at[0, "job_id"] == N.compute_job_id("acme", "ai engineer")
    assert stats.by_rule == {"hash": 1} and stats.new_job_ids == [store.at[0, "job_id"]]


def test_same_pair_elsewhere_reuses_the_id_even_when_it_is_not_the_hash():
    store = S.coerce_store(pd.DataFrame([{**post(GH.format(1)), "job_id": "inherit1",
                                           "first_seen": TODAY, "last_seen": TODAY}]))
    li = post("https://www.linkedin.com/jobs/view/99", source="linkedin")
    store, stats = fold(store, li)
    assert set(store["job_id"]) == {"inherit1"} and stats.by_rule == {"pair": 1}
    assert stats.new_job_ids == []


def test_a_known_direct_url_joins_its_posting():
    store, _ = fold(S.empty_store(), post(GH.format(1)))
    li = post(GH.format(1) + "?src=LinkedIn", company="Acme Corp Hiring", title="AI Engineer (Remote)",
              source="linkedin", key_url="https://www.linkedin.com/jobs/view/5")
    store, stats = fold(store, li)
    assert store["job_id"].nunique() == 1 and stats.by_rule == {"url": 1}


def test_fuzzy_joins_the_same_role_from_another_source():
    store, _ = fold(S.empty_store(), post(GH.format(1), company="Hilbert", title="Senior AI Engineer"))
    li = post("https://www.linkedin.com/jobs/view/7", company="Hilbert's AI",
              title="Senior AI Engineer, Remote", source="linkedin", jd=JD + " Travel.")
    store, stats = fold(store, li)
    assert store["job_id"].nunique() == 1 and stats.by_rule == {"fuzzy": 1}


def test_fuzzy_never_merges_two_postings_of_one_board():
    store, _ = fold(S.empty_store(), post(GH.format(1), title="Applied AI Architect"))
    other = post(GH.format(2), title="Applied AI Architect Enterprise")
    store, _ = fold(store, other)
    assert store["job_id"].nunique() == 2


@pytest.mark.parametrize("a,b", [("AI Engineer II", "AI Engineer III"),
                                 ("Senior AI Engineer", "Staff AI Engineer")])
def test_fuzzy_never_merges_two_levels(a, b):
    store, _ = fold(S.empty_store(), post(GH.format(1), title=a))
    store, _ = fold(store, post("https://www.linkedin.com/jobs/view/8", title=b, source="linkedin"))
    assert store["job_id"].nunique() == 2


def test_fuzzy_needs_jd_evidence():
    store, _ = fold(S.empty_store(), post(GH.format(1), company="Goal Solutions"))
    store, _ = fold(store, post("https://www.linkedin.com/jobs/view/9", company="Shakti Solutions",
                                source="linkedin", jd=JD_OTHER))
    assert store["job_id"].nunique() == 2


def test_a_hash_collision_drops_the_newcomer(monkeypatch):
    store, _ = fold(S.empty_store(), post(GH.format(1)))
    monkeypatch.setattr(N, "compute_job_id", lambda c, t: store.at[0, "job_id"])
    store2, stats = fold(store, post(GH.format(2), company="Zeta", title="Chef",
                                     jd=JD_OTHER, source="lever"))
    assert len(store2) == 1 and len(stats.collisions) == 1


def test_blank_company_is_dropped_not_hashed():
    _, stats = fold(S.empty_store(), post("https://careers.example.com/1", company=""))
    assert stats.dropped_blank == 1


def test_blank_company_is_recovered_from_the_board_slug():
    row = post("https://job-boards.greenhouse.io/edison-scientific/jobs/3", company="")
    assert row["company"] == "Edison Scientific"


def test_two_new_postings_in_one_night_agree():
    a = post(GH.format(1))
    b = post("https://www.linkedin.com/jobs/view/11", source="linkedin")
    store, stats = fold(S.empty_store(), a, b)
    assert store["job_id"].nunique() == 1 and len(stats.new_job_ids) == 1


# ---------- refresh, touch, repost ----------

def test_refresh_keeps_id_and_first_seen_and_takes_the_newer_jd():
    store, _ = fold(S.empty_store(), post(GH.format(1)), today=TODAY - pd.Timedelta(days=5))
    jid, first = store.at[0, "job_id"], store.at[0, "first_seen"]
    store, stats = fold(store, post(GH.format(1), jd=JD + " New benefits."))
    assert store.at[0, "job_id"] == jid and store.at[0, "first_seen"] == first
    assert store.at[0, "last_seen"] == TODAY and store.at[0, "jd_text"].endswith("benefits.")
    assert stats.refreshed == 1


def test_an_empty_jd_never_blanks_a_stored_one():
    store, _ = fold(S.empty_store(), post(GH.format(1)))
    store, _ = fold(store, post(GH.format(1), jd=""))
    assert store.at[0, "jd_text"] == JD


def test_touch_moves_last_seen_only():
    store, _ = fold(S.empty_store(), post(GH.format(1)), today=TODAY - pd.Timedelta(days=3))
    key = store.at[0, "posting_key"]
    store, stats = fold(store, touched=[key])
    assert store.at[0, "last_seen"] == TODAY and stats.touched == 1


def test_a_repost_after_the_live_window_restarts_first_seen():
    store, _ = fold(S.empty_store(), post(GH.format(1)), today=TODAY - pd.Timedelta(days=40))
    store, stats = fold(store, touched=[store.at[0, "posting_key"]])
    assert store.at[0, "first_seen"] == TODAY and stats.reposted == 1


def test_forget_keeps_tracked_rows():
    store, _ = fold(S.empty_store(), post(GH.format(1)), post(GH.format(2), title="Chef", jd=JD_OTHER),
                    today=TODAY - pd.Timedelta(days=200))
    keep_id = store.at[0, "job_id"]
    out, n = S.forget(store, TODAY, 120, frozenset({keep_id}))
    assert n == 1 and list(out["job_id"]) == [keep_id]


def test_flush_round_trips(tmp_path):
    path = tmp_path / "jobs.parquet"
    batch = S.Batch()
    batch.upsert(post(GH.format(1)))
    S.flush(batch, TODAY, frozenset(), 14, path=path)
    batch.touch(S.load_store(path).at[0, "posting_key"])
    stats = S.flush(batch, TODAY, frozenset(), 14, path=path)
    assert stats.touched == 1 and len(S.load_store(path)) == 1


# ---------- the live view ----------

class AllowAll:
    def check(self, raw, vertical="", title=""):
        if raw == "Paris, France":
            return Allowed(False, raw, True)
        return Allowed(True, raw or "", bool(raw))


def view(store, state_index=None, scores=None, today=TODAY):
    inp = S.ViewInputs(today=today, state_index=state_index or {}, scores=scores or {},
                       live_days=14, search_max_age_days=14, score_threshold=80,
                       high_days=60, low_days=15)
    return S.build_view(store, AllowAll(), inp)


def at(days_ago):
    return TODAY - pd.Timedelta(days=days_ago)


def test_view_has_the_clean_columns_one_row_per_job():
    store, _ = fold(S.empty_store(), post(GH.format(1), location_raw="Austin, TX"),
                    post("https://www.linkedin.com/jobs/view/1", source="linkedin"))
    out, funnel = view(store)
    assert list(out.columns) == S.CLEAN_COLUMNS
    assert len(out) == 1 and funnel["jobs"] == 1


def test_view_drops_postings_not_seen_within_the_live_window():
    store, _ = fold(S.empty_store(), post(GH.format(1)), today=at(20))
    out, funnel = view(store)
    assert out.empty and funnel["live"] == 0


def test_view_cuts_old_search_rows_but_not_board_rows():
    store, _ = fold(S.empty_store(),
                    post(GH.format(1), posted_date=at(90)),
                    post("https://www.linkedin.com/jobs/view/2", title="Data Engineer",
                         source="linkedin", posted_date=at(30)))
    out, _ = view(store)
    assert list(out["source"]) == ["greenhouse"]


def test_view_applies_the_location_allowlist():
    store, _ = fold(S.empty_store(), post(GH.format(1), location_raw="Paris, France"))
    out, funnel = view(store)
    assert out.empty and funnel["after_location"] == 0


def test_view_retention_tiers():
    rows = [post(GH.format(i), title=t, jd=JD + t) for i, t in
            enumerate(["AI Engineer", "ML Engineer", "Data Engineer"], start=1)]
    store, _ = fold(S.empty_store(), *rows, today=at(30))
    store["last_seen"] = TODAY
    ids = dict(zip(store["title"], store["job_id"]))
    scores = {ids["AI Engineer"]: 90.0, ids["ML Engineer"]: 40.0}
    out, _ = view(store, scores=scores)
    # high score (60d) and unscored (60d) stay at 30 days; low (15d) is gone.
    assert set(out["title"]) == {"AI Engineer", "Data Engineer"}


def test_view_brings_back_active_roles_and_tags_state():
    store, _ = fold(S.empty_store(), post(GH.format(1)), today=at(100))
    jid = store.at[0, "job_id"]
    out, funnel = view(store, state_index={jid: {"job_id": jid, "state": "applied"}})
    assert list(out["job_id"]) == [jid] and funnel["active_added"] == 1
    assert out.at[0, "application_status"] == "applied"


def test_view_representative_prefers_an_applyable_url_then_the_newest():
    li = post("https://www.linkedin.com/jobs/view/1", source="linkedin", jd=JD + " longer" * 50)
    gh = post(GH.format(1))
    store, _ = fold(S.empty_store(), li, gh)
    out, _ = view(store)
    assert out.at[0, "url"] == GH.format(1)


def test_view_is_deterministic():
    rows = [post(GH.format(i), title=f"AI Engineer {i}", jd=JD + str(i)) for i in range(20)]
    store, _ = fold(S.empty_store(), *rows)
    a, _ = view(store)
    b, _ = view(store.sample(frac=1, random_state=3))
    pd.testing.assert_frame_equal(a, b)
