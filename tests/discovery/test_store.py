"""store.py with dedupe.py and live_view.py: job_id rules, refresh/touch, and the live view."""
from __future__ import annotations

import pandas as pd
import pytest

from src.shared import identity
from src.discovery import store as S
from src.discovery import dedupe, live_view
from src.shared.location.allowlist import Allowed

TODAY = pd.Timestamp("2026-09-27")
JD = "Build retrieval pipelines for a clinical trials platform and ship them. " * 10
JD_OTHER = "Run the warehouse shift schedule and own inventory accuracy end to end. " * 10


def post(url, company="Acme", title="AI Engineer", source="greenhouse", jd=JD, **kw):
    return S.make_posting(url=url, company=company, title=title, source=source,
                          jd_text=jd, vertical=kw.pop("vertical", "primary"), **kw)


def fold(store, *rows, touched=(), tracked=frozenset(), today=TODAY, live_days=14, trim=dedupe.NO_TRIM):
    batch = S.Batch()
    for r in rows:
        batch.upsert(r)
    for k in touched:
        batch.touch(k)
    return S.apply_batch(store, batch, today, tracked, live_days, trim)


GH = "https://job-boards.greenhouse.io/acme/jobs/{}"


# ---------- id rules ----------

def test_new_posting_gets_the_hash():
    store, stats = fold(S.empty_store(), post(GH.format(1)))
    assert store.at[0, "job_id"] == identity.compute_job_id("acme", "ai engineer")
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
    monkeypatch.setattr(identity, "compute_job_id", lambda c, t: store.at[0, "job_id"])
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


# ---------- the same role elsewhere: names, JD order, maybes ----------

ROLE = ("## The Role\n\nYou will build retrieval pipelines, evaluation harnesses and agent tooling "
        "for underwriting work, shipping weekly to real customers with a small expert team.\n\n")
REQ = ("## Requirements\n\nThree years of Python, production LLM systems, strong SQL and a habit "
       "of measuring everything you ship before and after release.\n\n")
ABOUT = ("## About Vouch\n\nVouch is the insurance broker that powers ambition, purpose built for "
         "growing technology companies and their leaders across the country.\n\n")
LI = "https://www.linkedin.com/jobs/view/{}"


def _pair(a, b, **kw):
    store, _ = fold(S.empty_store(), a, **kw)
    return fold(store, b, **kw)


def test_the_same_jd_with_its_sections_reordered_merges():
    store, stats = _pair(post(LI.format(1), company="Vouch Insurance", source="linkedin",
                              jd=ROLE + REQ + ABOUT),
                         post("https://ats.rippling.com/vouch-inc/jobs/a7099d11-3586-4b85-9d0c-904c9203c8d0",
                              company="Vouch, Inc", source="stapply", jd=ABOUT + REQ + ROLE))
    assert store["job_id"].nunique() == 1 and stats.by_rule == {"fuzzy": 1}


@pytest.mark.parametrize("a,b", [("vouch", "vouch insurance"), ("ifs", "ifs1"), ("frost", "frostbank"),
                                 ("red bull", "redbull"), ("hartford", "thehartford"),
                                 ("hilbert", "hilbert s ai"), ("radiance technologies", "radiancetech")])
def test_one_employer_under_two_spellings(a, b):
    assert dedupe.same_company(a, b) and dedupe.same_company(b, a)


@pytest.mark.parametrize("a,b", [("x", "xander"), ("apex", "apexa"), ("stripe", "stripes"),
                                 ("vouch", "voucher"), ("vouch", "vouchr"), ("ai", "ai solutions"),
                                 ("goal solutions", "shakti solutions"), ("", "acme")])
def test_near_spellings_are_two_employers(a, b):
    assert not dedupe.same_company(a, b)


def test_two_sites_of_one_source_merge_but_one_board_never_does():
    store, _ = _pair(post(GH.format(1), company="Vouch", source="stapply"),
                     post("https://jobs.lever.co/vouch-insurance/636fc05c-d348-4a3a-9f0f-2e1a2b3c4d5e",
                          company="Vouch Insurance", source="stapply"))
    assert store["job_id"].nunique() == 1
    store, _ = _pair(post(GH.format(1), company="Vouch"), post(GH.format(2), company="Vouch Insurance"))
    assert store["job_id"].nunique() == 2


def test_unrelated_names_with_one_jd_are_recorded_not_merged():
    store, stats = _pair(post(GH.format(1), company="Capgemini"),
                         post(LI.format(2), company="Sogeti", source="linkedin"))
    assert store["job_id"].nunique() == 2
    [m] = stats.maybe_duplicates
    assert m["reason"] == "other_name" and m["jd_score"] == 100
    assert m["pair"] == "|".join(sorted(store["job_id"])) and m["date"] == "2026-09-27"


def test_related_names_with_a_weaker_jd_are_recorded_not_merged():
    other = ROLE + ("## Requirements\n\nFive years of Java, distributed data platforms, Kafka "
                    "streams, on call duty and a habit of measuring before release.\n\n") + ABOUT
    store, stats = _pair(post(GH.format(1), company="Vouch", jd=ROLE + REQ + ABOUT),
                         post(LI.format(3), company="Vouch Insurance", source="linkedin", jd=other))
    assert store["job_id"].nunique() == 2
    assert [m["reason"] for m in stats.maybe_duplicates] == ["weak_jd"]


def test_titles_naming_another_team_are_recorded_not_merged():
    store, stats = _pair(post(GH.format(1), company="OpenAI", title="Applied AI Engineer"),
                         post(LI.format(4), company="OpenAI Inc", title="Applied AI Engineer, Codex",
                              source="linkedin"))
    assert store["job_id"].nunique() == 2
    assert [m["reason"] for m in stats.maybe_duplicates] == ["other_title"]


def test_a_title_that_only_adds_its_city_or_workplace_merges():
    store, _ = _pair(post(GH.format(1), company="OpenAI", title="Applied AI Engineer",
                          location_raw="Tokyo, Japan"),
                     post(LI.format(5), company="OpenAI Inc", title="Applied AI Engineer, Tokyo (Hybrid)",
                          source="linkedin", location_raw="Tokyo, Japan"))
    assert store["job_id"].nunique() == 1


BOILER = ("## Benefits\n\n" + "Medical, dental and vision cover, a learning budget, a home office "
          "stipend, paid parental leave and flexible hours. " * 10
          + "\n\n## Equal Opportunity\n\n" + "We are an equal opportunity employer. " * 10)


def test_shared_boilerplate_never_makes_two_roles_one():
    a = "## The Role\n\n" + "Own the warehouse shift schedule and the weekly cycle counts at both sites. " * 3
    b = "## The Role\n\n" + "Build retrieval pipelines and evaluation harnesses for clinical trials. " * 3
    trim = (("benefits", "equal opportunity"), ("visa",))
    plain = dedupe.jd_key(a + "\n\n" + BOILER), dedupe.jd_key(b + "\n\n" + BOILER)
    assert dedupe.fuzz.ratio(*plain) >= dedupe.JD_MERGE
    store, stats = _pair(post(GH.format(1), company="Acme", jd=a + "\n\n" + BOILER),
                         post(LI.format(6), company="Acme Labs", source="linkedin", jd=b + "\n\n" + BOILER),
                         trim=trim)
    assert store["job_id"].nunique() == 2 and stats.maybe_duplicates == []


def test_a_maybe_pair_is_written_once_across_flushes(tmp_path):
    path = tmp_path / "jobs.parquet"
    for row in (post(GH.format(1), company="Capgemini"), post(LI.format(2), company="Sogeti", source="linkedin"),
                post(LI.format(7), company="Sogeti", source="linkedin")):
        batch = S.Batch()
        batch.upsert(row)
        S.flush(batch, TODAY, frozenset(), 14, path=path)
    rows = S.load_maybe_duplicates(tmp_path / "maybe_duplicates.jsonl")
    assert [r["reason"] for r in rows] == ["other_name"]


def test_merged_stats_keep_each_pair_once():
    a, b = S.ApplyStats(), S.ApplyStats()
    a.maybe_duplicates = [{"pair": "a|b"}]
    b.maybe_duplicates = [{"pair": "a|b"}, {"pair": "a|c"}]
    a.merge(b)
    assert [m["pair"] for m in a.maybe_duplicates] == ["a|b", "a|c"]


def test_the_backfill_finds_two_stored_ids_of_one_role():
    from src.discovery.dedupe import find_pairs
    rows = [{**post(LI.format(1), company="Vouch Insurance", source="linkedin", jd=ROLE + REQ + ABOUT),
             "job_id": "aaaa1111"},
            {**post("https://jobs.lever.co/vouch/636fc05c-d348-4a3a-9f0f-2e1a2b3c4d5e", company="Vouch",
                    source="lever", jd=ABOUT + REQ + ROLE), "job_id": "bbbb2222"},
            {**post(GH.format(9), company="Zeta", title="Chef", jd=JD_OTHER), "job_id": "cccc3333"}]
    store = S.coerce_store(pd.DataFrame(rows).assign(first_seen=TODAY, last_seen=TODAY))
    [pair] = find_pairs(store, date="2026-09-27")
    assert pair["pair"] == "aaaa1111|bbbb2222" and pair["reason"] == "backfill_merge"


# ---------- ids merged before the store ----------

CODEX = identity.compute_job_id("openai", "applied ai engineer codex")


def _over_merged(*extra):
    """CODEX holds its own role plus a row of another team, as an id merged
    before the store can."""
    rows = [{**post("https://jobs.ashbyhq.com/openai/1", company="OpenAI", title="Applied AI Engineer, Codex",
                    source="ashby"), "job_id": CODEX},
            {**post(LI.format(20), company="OpenAI", title="Applied AI Engineer, Cyber", source="linkedin"),
             "job_id": CODEX}, *extra]
    return S.coerce_store(pd.DataFrame(rows).assign(first_seen=TODAY - pd.Timedelta(days=30),
                                                    last_seen=TODAY))


def test_a_pair_held_by_another_roles_id_gets_its_own_id():
    cyber = post(GH.format(21), company="OpenAI", title="Applied AI Engineer, Cyber")
    store, stats = fold(_over_merged(), cyber, tracked=frozenset({CODEX}))
    jid = identity.compute_job_id("openai", "applied ai engineer cyber")
    assert stats.by_rule == {"hash": 1} and stats.new_job_ids == [jid]
    [m] = stats.maybe_duplicates
    assert m["pair"] == "|".join(sorted((jid, CODEX))) and m["reason"] == "other_title"
    again = post("https://jobs.lever.co/openai/636fc05c-d348-4a3a-9f0f-2e1a2b3c4d5e", company="OpenAI",
                 title="Applied AI Engineer, Cyber", source="lever")
    store, stats = fold(store, again, tracked=frozenset({CODEX}), today=TODAY + pd.Timedelta(days=1))
    assert stats.by_rule == {"pair": 1} and store.iloc[-1]["job_id"] == jid


def test_fuzzy_never_joins_another_roles_id():
    near = post(GH.format(22), company="OpenAI Inc", title="Applied AI Engineer, Cyber (Remote)")
    store, stats = fold(_over_merged(), near)
    assert stats.by_rule == {"hash": 1} and CODEX not in set(store.iloc[-1:]["job_id"])
    assert {m["other"] for m in stats.maybe_duplicates} == {CODEX}


def test_an_ids_own_role_still_joins_it_from_another_city():
    sf = {**post(LI.format(23), company="OpenAI", title="Applied AI Engineer, Codex, San Francisco",
                 source="linkedin", location_raw="San Francisco, CA"), "job_id": CODEX}
    again = post(GH.format(24), company="OpenAI", title="Applied AI Engineer, Codex, San Francisco",
                 location_raw="San Francisco, CA")
    store, stats = fold(_over_merged(sf), again)
    assert stats.by_rule == {"pair": 1} and store.iloc[-1]["job_id"] == CODEX


def test_a_name_variant_inside_an_id_still_joins_it():
    own = identity.compute_job_id("vouch insurance", "ai engineer")
    rows = [{**post(LI.format(25), company="Vouch Insurance", source="linkedin"), "job_id": own},
            {**post(GH.format(26), company="Vouch, Inc"), "job_id": own}]
    store = S.coerce_store(pd.DataFrame(rows).assign(first_seen=TODAY, last_seen=TODAY))
    store, stats = fold(store, post("https://jobs.lever.co/vouch/1", company="Vouch, Inc", source="lever"))
    assert stats.by_rule == {"pair": 1} and set(store["job_id"]) == {own}


# ---------- refresh, touch, repost ----------

def test_refresh_keeps_id_and_first_seen_and_takes_the_newer_jd():
    store, _ = fold(S.empty_store(), post(GH.format(1)), today=TODAY - pd.Timedelta(days=5))
    jid, first = store.at[0, "job_id"], store.at[0, "first_seen"]
    store, stats = fold(store, post(GH.format(1), jd=JD + " New benefits."))
    assert store.at[0, "job_id"] == jid and store.at[0, "first_seen"] == first
    assert store.at[0, "last_seen"] == TODAY and store.at[0, "jd_text"].endswith("benefits.")
    assert stats.refreshed == 1


def test_a_refresh_updates_the_shown_name_but_never_the_id_pair():
    """A later copy may name the company better ("whatfix101" -> "Whatfix");
    the normalized name the id was built from stays."""
    store, _ = fold(S.empty_store(), post(GH.format(1), company="whatfix101"))
    jid = store.at[0, "job_id"]
    store, _ = fold(store, post(GH.format(1), company="Whatfix", source="stapply"))
    assert (store.at[0, "company"], store.at[0, "company_normalized"]) == ("Whatfix", "whatfix101")
    assert store.at[0, "job_id"] == jid


def test_a_refresh_from_another_source_does_not_clash_with_its_own_id():
    """The refreshed posting and a new copy of the same role elsewhere keep
    agreeing on the pair, so the newcomer is not dropped as a collision."""
    store, _ = fold(S.empty_store(), post(GH.format(1), company="Telnyx", title="Forward Deployed Engineer"))
    other = post("https://www.welcometothejungle.com/en/companies/x/jobs/fde", company="Telnyx",
                 title="Forward Deployed Engineer", source="stapply", jd=JD_OTHER)
    store, stats = fold(store, post(GH.format(1), company="telnyx54", source="stapply"), other)
    assert stats.collisions == [] and store["job_id"].nunique() == 1


def test_the_board_is_the_urls_ats_whoever_finds_it():
    assert post(GH.format(1), source="stapply")["board"] == "greenhouse|acme"
    assert post("https://www.linkedin.com/jobs/view/9", source="linkedin")["board"] == ""


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
    inp = live_view.ViewInputs(today=today, state_index=state_index or {}, scores=scores or {},
                       live_days=14, search_max_age_days=14, score_threshold=80,
                       high_days=60, low_days=15)
    return live_view.build_view(store, AllowAll(), inp)


def at(days_ago):
    return TODAY - pd.Timedelta(days=days_ago)


def test_view_has_the_clean_columns_one_row_per_job():
    store, _ = fold(S.empty_store(), post(GH.format(1), location_raw="Austin, TX"),
                    post("https://www.linkedin.com/jobs/view/1", source="linkedin"))
    out, funnel = view(store)
    assert list(out.columns) == live_view.CLEAN_COLUMNS
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
