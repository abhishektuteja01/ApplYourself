"""summary.md: each section from the records, and 'tonight' as time, not date."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pandas as pd

from src import summary as M
from src import synonyms_cli
from src.discovery import digest

SINCE = datetime(2026, 9, 28, 15, 45)


def test_since_comes_from_the_run_folder_name(tmp_path):
    d = tmp_path / "pipeline_2026-09-28_154500"
    assert M.since_of(d) == SINCE
    assert M.since_of(None).time() == datetime.min.time()


def test_the_latest_run_folder_ignores_anything_that_is_not_a_night(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "LOGS", tmp_path)
    assert M.latest_run_dir() is None
    for name in ("pipeline_2026-09-27_154500", "pipeline_2026-09-28_154500",
                 "pipeline_test_2026-09-29_120000", "discovery_2026-09-29_120000.log"):
        (tmp_path / name).mkdir()
    assert M.latest_run_dir().name == "pipeline_2026-09-28_154500"


def test_a_failed_stage_leads_and_a_missing_one_says_so():
    stages = {"discover": {"code": 0, "seconds": 11400, "detail": ""},
              "score": {"code": 1, "seconds": 90, "detail": "still owed"}}
    out = M.run_section(stages)
    assert out[0] == "**score failed** (code 1) still owed"
    assert out[-1] == ("Stages: discover ok 3h10 · score FAILED · synonyms not run · "
                       "tailor not run · apply_prep not run")


def test_a_partial_stage_and_its_deferred_roles_are_shown(tmp_path):
    (tmp_path / "summary.txt").write_text(
        "discover code=0 seconds=60 \n"                       # a line from before status=
        "tailor code=0 seconds=60 status=partial deferred=56 30/86 tailored, 56 deferred\n",
        encoding="utf-8")
    stages = M.stage_lines(tmp_path)
    assert stages["discover"]["status"] == "ok" and stages["tailor"]["detail"] == "30/86 tailored, 56 deferred"
    out = M.run_section(stages)
    assert out[0] == "**tailor partial** 30/86 tailored, 56 deferred"
    assert "tailor partial 1m" in out[1] and out[2].endswith(": tailor 56")


def _report(run_id, new, lanes=None, clean=3000):
    return {"run_id": run_id, "new_job_ids": new, "view": {"clean": clean},
            "lanes": lanes or {"lever": {"status": "ok", "errors": [], "http": {}}}}


def test_discovery_counts_this_run_only_and_names_every_kind_of_error():
    lanes = {"ashby": {"status": "ok", "errors": ["Acme: 'Eng': '_id'"], "truncated": True,
                       "http": {"429": 7}},
             "workday": {"status": "crashed", "crash": "Traceback\nValueError: boom"}}
    reports = [_report("2026-09-29_0100", ["b", "c"], lanes),
               _report("2026-09-28_1600", ["a", "b"]),
               _report("2026-09-27_1600", ["z"])]
    out = M.discovery_section(reports, SINCE)
    assert out[0] == "3 new postings, 3000 live."
    text = "\n".join(out)
    for want in ("ashby: Acme", "cut short", "7 x HTTP 429", "workday crashed: ValueError: boom"):
        assert want in text
    assert M.discovery_section([_report("2026-09-20_1600", ["z"])], SINCE) \
        == ["No discovery report for this run."]
    assert M.discovery_section([_report("2026-09-28_1600", [])], SINCE)[1] == "Errors: none"


def test_errors_are_capped_with_a_count():
    out = M._errors([f"e{i}" for i in range(8)])
    assert out[-1] == "- …and 3 more" and len(out) == 1 + M.ERRORS_SHOWN + 1


def _scored(rows):
    return pd.DataFrame([{"scored_at": at, "scored_by_model": model, "suggested_action": act}
                         for at, model, act in rows])


def test_scoring_counts_tonights_rows_and_flags_an_open_run():
    df = _scored([(SINCE + timedelta(hours=8), "score-judge:sonnet", "tailor"),
                  (SINCE + timedelta(hours=8), "score-judge:sonnet", "skip"),
                  (SINCE + timedelta(hours=8), "filter:years", "skip"),
                  (SINCE - timedelta(days=1), "score-judge:sonnet", "tailor")])
    out = M.scoring_section(df, SINCE, "merged=2 pruned=0 keepers=75", 0, 0)
    assert out[0] == "2 judged: 1 tailor, 0 manual-review, 1 skip. Filtered: years 1. Keepers 75."
    assert out[1] == "Errors: none"
    bad = "\n".join(M.scoring_section(df, SINCE, "x", 3, 1))
    assert "3 slices owed" in bad and "stage exited 1" in bad
    assert M.scoring_section(None, SINCE, "", 0, None)[0] == "Nothing scored."


def test_synonyms_lists_only_the_phrases_per_target_with_blank_lines_between_blocks():
    added = [{"target": "B-A-01", "phrase": "p1", "keyword": "k1"},
             {"target": "B-A-01", "phrase": "p2", "keyword": "k2"},
             {"target": "SKILL-X", "phrase": "p3", "keyword": ""}]
    declined = [{"keyword": f"kw{i}", "reason": f"why{i}"} for i in range(9)]
    opened = [{"id": "U1", "thing": "Airflow", "context": "Acme", "via": "the nightly DAGs"}]
    out = M.synonyms_section(added, declined, opened)
    assert out[:3] == ["**Added (3)**", "- B-A-01: p1, p2", "- SKILL-X: p3"]
    assert out[3] == "" and out[4].startswith("**Declined")
    assert sum(line.startswith("- kw") for line in out) == 9
    assert "" in out[out.index("- kw8: why8"):] and "**Questions for you (1)**" in out
    assert "- [U1] Did you work on Airflow during Acme? You may have used it via the nightly DAGs." in out
    assert "k1" not in "\n".join(out)
    empty = M.synonyms_section([], [], [])
    assert empty == ["**Added:** none", "", "**Declined:** none new", "", "**Questions:** none"]


def test_tonight_uses_the_timestamp_and_crosses_midnight():
    rows = [{"at": "2026-09-28T22:00:00", "n": "before"},
            {"at": "2026-09-29T01:10:00", "n": "after midnight"},
            {"date": "2026-09-28", "n": "no stamp, same day"},
            {"date": "2026-09-27", "n": "no stamp, old"}]
    got = [r["n"] for r in M._tonight(rows, SINCE.replace(hour=23), "date")]
    # A row with no timestamp (older data) can only be judged by its date.
    assert got == ["after midnight", "no stamp, same day"]
    assert [r["n"] for r in M._tonight(rows, SINCE, "date")] == \
        ["before", "after midnight", "no stamp, same day"]


def test_tailoring_lists_every_unresolved_role_with_its_reason_uncapped():
    bad = {f"r{i}": f"why {i}" for i in range(9)}
    out = M.tailoring_section(12, bad, {"r3": "Beta — Eng"}, "", 0)
    assert out[0] == "3 of 12 tailored."
    assert sum(line.startswith("- r") for line in out) == 9 and "- r3 Beta — Eng: why 3" in out
    assert not any("more" in line for line in out)
    assert M.tailoring_section(0, {}, {}, "", None) == ["Nothing to tailor.", "Errors: none"]


def test_the_reason_a_role_has_no_resume_is_read_from_what_is_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "APPLICATIONS", tmp_path / "applications")
    lane = tmp_path / "applications" / "lane"
    (lane / "2026-09-29_a_x_aaaaaaaa").mkdir(parents=True)
    (lane / "2026-09-29_a_x_aaaaaaaa" / "N_Resume.docx").write_bytes(b"d")
    assert M.tailor_reason("aaaaaaaa", None, {"aaaaaaaa"}) == \
        "resume built, PDF missing (Word conversion failed)"
    (lane / "2026-09-29_a_x_aaaaaaaa" / "N_Resume.pdf").write_bytes(b"p")
    assert M.tailor_reason("aaaaaaaa", None, {"aaaaaaaa"}) == ""
    assert "posting is gone" in M.tailor_reason("bbbbbbbb", None, set())
    run = tmp_path / "run"
    run.mkdir()
    (run / "tailor_cccccccc.json").write_text(json.dumps({"result": "I stopped: app new failed"}),
                                              encoding="utf-8")
    assert M.tailor_reason("cccccccc", run, {"cccccccc"}) == "tailor stopped: I stopped: app new failed"
    assert M.tailor_reason("dddddddd", run, {"dddddddd"}) == "no resume built and no reason recorded"


def _plan(**kw):
    return {"draftable": [], "unmapped": [], "fields": [], **kw}


def _guess(label, value, key):
    return {"label": label, "value": value, "source": f"bank:{key}~{key}"}


def test_open_items_sort_into_guesses_questions_and_missing_values_with_a_tally_per_role():
    snaps = [("a1", _plan(fields=[_guess("LinkedIn URL", "https://li/a", "linkedin"),
                                  {"label": "Pronouns", "source": "bank:pronouns"}],
                          draftable=[{"label": "Tell us about AI use", "options": []}],
                          work_authorization={"job_country": "India"}, out_dir="applications/ai_eng/x")),
             ("b2", _plan(fields=[_guess("Linkedin Profile ✱", "https://li/a", "linkedin"),
                                  _guess("Your LinkedIn", "https://li/b", "linkedin")],
                          unmapped=[{"label": "Salary", "required": True, "tier": "B",
                                     "reason": "salary: no figure for a role in India (verticals.yaml salary_expectation)",
                                     "options": []},
                                    {"label": "Old", "required": True, "tier": "C", "options": ["Y"]},
                                    {"label": "Optional thing", "required": False}]))]
    got = M.open_items(snaps)
    assert [g["label"] for g in got["guesses"]] == ["LinkedIn URL"]
    assert list(got["guesses"][0]["values"]) == ["https://li/a", "https://li/b"]
    assert got["guesses"][0]["values"]["https://li/a"]["roles"] == ["a1", "b2"]
    assert {q["label"] for q in got["asks"]} == {"Tell us about AI use", "Old"}
    assert [f["roles"] for f in got["facts"]] == [["b2"]]
    assert got["by_role"] == {"a1": {"guesses": 1, "asks": 1, "facts": 0},
                              "b2": {"guesses": 2, "asks": 1, "facts": 1}}
    listed = M.open_items([("c3", _plan(fields=[
        {"label": "Pronouns", "value": ["He/him"], "source": "bank:pronouns~pronouns"}]))])
    assert list(listed["guesses"][0]["values"]) == ["He/him"]


def test_why_a_role_needs_you_is_said_in_plain_counts():
    assert M._why({"guesses": 1, "asks": 2, "facts": 1}) == \
        "1 guess to confirm, 2 questions to answer, 1 value missing"
    assert M._why({"guesses": 0, "asks": 0, "facts": 0}) == "drafted answers wait in its folder (answers_override.json), nothing else open"


def test_missing_values_are_worded_for_a_person():
    assert M._fact_text("salary: no figure for a role in India (verticals.yaml salary_expectation)") \
        .startswith("Salary for a role in India: no figure set")
    assert "start_in_days" in M._fact_text("start date: no start_in_days set for Germany (work_authorization.countries)")
    assert M._fact_text("consent: none of ['Yes', 'I agree'] is offered (['No'])") == \
        "The saved consent answer is not one of the options offered"
    assert M._fact_text("something new and odd") == "something new and odd"


def _row(job, status, note=""):
    return {"name": job, "company": "Acme", "title": "Eng", "status": status, "note": note}


def _items(**kw):
    base = {"guesses": [], "asks": [], "facts": [], "by_role": {}, "roles": {}}
    return {**base, **kw}


def test_apply_starts_with_the_ready_ids_then_plain_errors_then_who_needs_you():
    rows = [_row("a1", "Submittable"), _row("z9", "Submittable"),
            _row("b2", "Needs your OK", "answers drafted in f"),
            _row("c3", "Parked", "failed: form changed"), _row("d4", "Expired", "expired"),
            _row("e5", "Resume ready"), _row("f6", "Parked", "1 question")]
    items = _items(by_role={"b2": {"guesses": 2, "asks": 0, "facts": 1}},
                   roles={"b2": {"company": "Beta", "title": "Eng", "url": "", "asks": [], "facts": []}})
    out = M.apply_section(rows, items, {"g7": "could not re-read the form (PostingExpired)"})
    assert out[0] == "Ready to submit: a1, z9" and out[1:3] == ["", M.CUES["ready"]]
    text = "\n".join(out)
    assert "- c3: failed: form changed" in out and "- d4: posting gone (expired), mark it skip or leave it" in out
    assert "- g7: could not re-read the form (PostingExpired)" in out
    assert M.CUES["apply_expired"] in out and M.CUES["apply_failed"] in out
    assert M.CUES["apply_unsent"] not in out
    assert "**Needs your OK (1)**" in out and "- b2 Beta — Eng: 2 guesses to confirm, 1 value missing" in out
    assert "e5" not in text
    quiet = M.apply_section([_row("a1", "Resume ready")], _items(roles={}), {})
    assert quiet == ["Ready to submit: none", "", "Errors: none", "", "**Answer bank:** nothing to add"]


def test_the_answer_bank_section_lists_every_label_and_every_role_uncut():
    label = "Are you currently subject to a non-compete agreement " + "x" * 200
    items = _items(
        guesses=[{"qid": "linkedin", "label": "LinkedIn", "values": {
                      "https://li": {"roles": ["a", "b", "c"], "countries": ["India", "United States"],
                                     "lanes": [], "labels": {"LinkedIn URL": ["a", "b"], "Linkedin": ["c"]}}}},
                 {"qid": "non_disclosure", "label": label, "values": {
                      "No": {"roles": ["a"], "countries": [], "lanes": [], "labels": {label: ["a"]}}}}],
        roles={"a": {"company": "Acme", "title": "Eng", "url": "https://x/a",
                     "asks": [{"label": "Twitter", "options": []},
                              {"label": "Which office?", "options": ["SF", "NYC"]}],
                     "facts": [{"label": "Desired Salary",
                                "reason": "salary: no figure for a role in India (verticals.yaml salary_expectation)"}]},
               "b": {"company": "Beta", "title": "Eng", "url": "", "asks": [], "facts": []}})
    out = M.apply_section([], items, {})
    assert "**Guesses to confirm (2): the bank matched these by keyword**" in out
    assert "- `linkedin` (LinkedIn) → https://li [India, United States]" in out
    assert '    - "LinkedIn URL": a, b' in out and '    - "Linkedin": c' in out
    assert f'    - "{label}": a' in out                     # a long label is never cut
    assert "**New questions and missing values, by role**" in out
    assert "- **a Acme — Eng** — https://x/a" in out
    assert '    - ask: "Twitter"' in out and '    - ask: "Which office?" (options: SF, NYC)' in out
    assert any(ln.startswith("    - missing: Salary for a role in India") and '"Desired Salary"' in ln
               for ln in out)
    assert "Beta" not in "\n".join(out)                   # nothing open there
    assert M.CUES["answers"] in out and M.CUES["missing"] in out


def test_a_missing_value_no_config_key_fills_gets_its_own_cue():
    why = "work authorization: names ['China', 'Iran']; pick by hand"
    items = _items(roles={"a": {"company": "Acme", "title": "Eng", "url": "", "asks": [],
                                "facts": [{"label": "Talent programs", "reason": why}]}},
                   facts=[{"reason": why, "roles": ["a"]}])
    out = M.apply_section([], items, {})
    assert M.CUES["missing_other"] in out and M.CUES["missing"] not in out


def test_a_board_rejection_is_not_listed_as_unsent():
    out = M.apply_section([_row("r1", "Rejected by board", "rejected by the board: duplicate")],
                          _items(roles={}), {})
    assert "- r1: rejected by the board: duplicate" in out
    assert M.CUES["apply_rejected"] in out and M.CUES["apply_unsent"] not in out


def test_every_waiting_role_is_listed():
    rows = [_row(f"w{i}", "Needs your OK", "n") for i in range(8)]
    out = M.apply_section(rows, _items(roles={}), {})
    assert "**Needs your OK (8)**" in out and sum(ln.startswith("- w") for ln in out) == 8
    assert "- w0: drafted answers wait in its folder (answers_override.json); not re-checked tonight: " \
        "`uv run apply plan w0 --questions --answers <that file>`" in out


def test_a_waiting_role_re_read_tonight_with_nothing_open_says_so():
    items = _items(by_role={"w0": {"guesses": 0, "asks": 0, "facts": 0}}, roles={})
    out = M.apply_section([_row("w0", "Needs your OK", "n")], items, {})
    assert "- w0: drafted answers wait in its folder (answers_override.json), nothing else open" in out


def test_an_error_from_an_earlier_night_is_listed_apart_with_its_date():
    rows = [_row("old1", "Parked", "failed: no option matching"), _row("new1", "Expired", "expired")]
    outcomes = {"old1": ("failed", "no option matching", "2026-08-24T18-15-48"),
                "new1": ("expired", "", (SINCE + timedelta(hours=2)).strftime("%Y-%m-%dT%H-%M-%S"))}
    out = M.apply_section(rows, _items(roles={}), {}, outcomes, SINCE)
    errors = out[out.index("Errors:"):out.index("**Still open from earlier nights (1)**")]
    assert "- new1: posting gone (expired), mark it skip or leave it" in errors
    assert not any("old1" in ln for ln in errors)
    assert "- old1 (2026-08-24): failed: no option matching" in out
    assert M.CUES["apply_failed"] in out and M.CUES["apply_expired"] in out


def test_an_expired_posting_reads_as_gone_not_as_a_traceback(tmp_path, monkeypatch):
    apps = tmp_path / "applications"
    monkeypatch.setattr(M, "APPLICATIONS", apps)
    d = apps / "lane" / "2026-09-29_acme_eng_a1b2c3d4"
    d.mkdir(parents=True)
    (d / "plan_after.json").write_text("", encoding="utf-8")
    (d / "plan_after.err").write_text("EXPIRED: posting 123 is gone (404): https://x\n",
                                      encoding="utf-8")
    _, bad = M._snapshots(SINCE - timedelta(days=1))
    assert bad == {"a1b2c3d4": "posting gone (expired)"}


def test_sheet_line_says_what_is_wrong_in_words_and_offers_the_fix():
    ok = {"finished": [], "duplicates": [], "not_live": [], "wrong_status": []}
    assert M.sheet_line(ok) == ["Sheet check: clean."]
    got = M.sheet_line({"finished": [], "duplicates": ["x"], "not_live": ["y", "z"]})
    assert got[0] == ("Sheet check: listed twice: x; no longer in the live listing, "
                      "the posting may be gone: y, z")
    assert got[1:] == ["", M.CUES["sheet"]]
    assert len(M.sheet_line({**ok, "duplicates": ["x"]})) == 1


def _world(tmp_path, monkeypatch):
    apps = tmp_path / "applications"
    for mod, name, value in ((M, "LOGS", tmp_path / "logs"), (M, "OUT", tmp_path / "summary.md"),
                             (M, "SCORED", tmp_path / "scored.parquet"),
                             (M, "CLEAN", tmp_path / "clean.parquet"),
                             (M, "STAGING", tmp_path / "staging"), (M, "APPLICATIONS", apps),
                             (synonyms_cli, "LOG", tmp_path / "log.jsonl"),
                             (synonyms_cli, "DECLINED", tmp_path / "declined.yaml"),
                             (synonyms_cli, "OPEN", tmp_path / "open.yaml"),
                             (M, "MAYBE_DUPES", tmp_path / "maybe_duplicates.jsonl")):
        monkeypatch.setattr(mod, name, value)
    monkeypatch.setattr(M.sheet, "rows", lambda: [_row("a1", "Submittable")])
    monkeypatch.setattr(M.sheet, "check", lambda: {"finished": [], "duplicates": [], "not_live": [], "wrong_status": []})
    return apps


def test_the_whole_summary_from_a_night_of_records(tmp_path, monkeypatch):
    apps = _world(tmp_path, monkeypatch)
    run = tmp_path / "logs" / "pipeline_2026-09-28_154500"
    run.mkdir(parents=True)
    (run / "summary.txt").write_text(
        "discover code=0 seconds=600 \nscore code=0 seconds=300 merged=2 keepers=9\n"
        "tailor code=0 seconds=60 1/2 tailored\n", encoding="utf-8")
    (run / "tailor_results.txt").write_text("a1 ok\nb2 fail\n", encoding="utf-8")
    pd.DataFrame([{"job_id": "b2", "company": "Beta", "title": "Eng"}]).to_parquet(M.CLEAN)
    _scored([("2026-09-29T01:00:00", "score-judge:x", "tailor")]).to_parquet(M.SCORED)
    reports = digest.JOBS_RUNS
    reports.mkdir(parents=True)
    (reports / "2026-09-28_1600.json").write_text(json.dumps(_report("2026-09-28_1600", ["a"])),
                                                  encoding="utf-8")
    synonyms_cli.LOG.write_text(json.dumps(
        {"date": "2026-09-29", "at": "2026-09-29T01:30:00", "target": "B-A-01",
         "phrase": "p", "keyword": "k"}) + "\n", encoding="utf-8")
    plan = apps / "lane" / "2026-09-29_acme_eng_a1b2c3d4"
    plan.mkdir(parents=True)
    (plan / "plan_after.json").write_text(json.dumps(_plan(draftable=[
        {"label": "Why us?", "options": []}])), encoding="utf-8")
    stale = apps / "lane" / "2026-09-01_old_eng_deadbeef"
    stale.mkdir(parents=True)
    (stale / "plan_after.json").write_text(json.dumps(_plan(draftable=[
        {"label": "Old question", "options": []}])), encoding="utf-8")
    os.utime(stale / "plan_after.json", (0, 0))
    broken = apps / "lane" / "2026-09-29_x_y_00000001"
    broken.mkdir(parents=True)
    (broken / "plan_after.json").write_text("", encoding="utf-8")
    (broken / "plan_after.err").write_text("x\nPostingExpired: gone\n", encoding="utf-8")

    (M.APPLICATIONS / "lane" / "2026-09-29_b_y_b2b2b2b2").mkdir(parents=True)
    assert M.main(["--run-dir", str(run)]) == 0
    text = M.OUT.read_text(encoding="utf-8")
    for want in ("# Night summary — 2026-09-28 (started 15:45)", "discover ok 10m",
                 "synonyms not run", "1 new postings", "1 judged: 1 tailor, 0 manual-review, 0 skip. Keepers 9.",
                 "- B-A-01: p", "1 of 2 tailored.", "b2 Beta — Eng: no resume built and no reason recorded",
                 "Ready to submit: a1", '    - ask: "Why us?"', "- 00000001: could not re-read the form (PostingExpired: gone)",
                 "Sheet check: clean."):
        assert want in text, want
    assert "Old question" not in text
    page = M.OUT.with_suffix(".html").read_text(encoding="utf-8")
    assert "<h2>Discovery</h2>" in page and 'class="cue"' in page and "Why us?" in page
    for command in ("uv run synonyms answer", "uv run track <id> skip", "uv run app build resume <id>"):
        assert command in text
    first = text
    (run / "tailor_results.txt").write_text("a1 ok\n", encoding="utf-8")
    assert M.main(["--run-dir", str(run)]) == 0
    assert M.OUT.read_text(encoding="utf-8") != first and not list(tmp_path.glob(".summary*"))


def test_the_html_is_one_mobile_page_and_escapes_the_records():
    md = ("# Night summary — 2026-09-28\n\n**score failed** (code 1) <boom> & more\n"
          "Stages: a ok\n\n## Apply\n- id1 Acme — Eng: `uv run x`\n- **bold** item\n\nplain\n")
    out = M.to_html(md)
    assert out.startswith("<!doctype html>") and 'name="viewport"' in out
    assert "<h1>Night summary — 2026-09-28</h1>" in out and "<h2>Apply</h2>" in out
    assert '<p class="fail"><strong>score failed</strong> (code 1) &lt;boom&gt; &amp; more</p>' in out
    assert "<ul><li>id1 Acme — Eng: <code>uv run x</code></li><li><strong>bold</strong> item</li></ul>" in out
    assert "<p>plain</p>" in out and "<script" not in out


def test_the_html_nests_one_level_and_styles_cue_lines():
    md = ("- **top** `x`\n    - \"a label\": r1, r2\n    - \"b\": r3\n- next\n\n"
          "**FIX:** do this: `uv run x`\n")
    out = M.to_html(md)
    assert ("<ul><li><strong>top</strong> <code>x</code><ul><li>\"a label\": r1, r2</li>"
            "<li>\"b\": r3</li></ul></li><li>next</li></ul>") in out
    assert '<p class="cue"><strong>FIX:</strong> do this: <code>uv run x</code></p>' in out


def test_a_yes_no_guess_on_a_follow_up_is_flagged_for_a_check():
    assert M._follow_up_guess("If yes, please provide your Deloitte employment details", "Yes")
    assert M._follow_up_guess("Please describe your experience", "No")
    assert not M._follow_up_guess("If yes, please provide details", "Some real text")
    assert not M._follow_up_guess("Are you currently employed by Deloitte?", "Yes")
    assert M._follow_up_guess("If you replied yes to the question above, please provide details", "No")
    assert not M._follow_up_guess("If you are not currently a resident of the location, "
                                  "would you be willing to relocate?", "Yes")
    items = _items(guesses=[{"qid": "deloitte", "label": "x", "values": {"Yes": {
        "roles": ["a"], "countries": [], "lanes": [], "labels": {
            "If yes, please provide details": ["a"], "Were you employed by Deloitte?": ["a"]}}}}])
    out = M.apply_section([], items, {})
    assert '    - "If yes, please provide details": a — **CHECK:** a follow-up that needs text, not "Yes"' in out
    assert '    - "Were you employed by Deloitte?": a' in out


def test_missing_values_of_one_kind_are_one_line_per_role_and_names_come_from_clean():
    why = "salary: no figure for a role in India (verticals.yaml salary_expectation)"
    items = _items(roles={"a": {"company": "slug-co", "title": "Eng", "url": "", "asks": [], "facts": [
        {"label": "Current Fixed", "reason": why}, {"label": "Desired Salary", "reason": why}]}},
        facts=[{"reason": why, "roles": ["a"]}], names={"a": "Real Co — Engineer"})
    out = M.apply_section([_row("a", "Needs your OK")], items, {})
    lines = [ln for ln in out if ln.startswith("    - missing")]
    assert len(lines) == 1 and '("Current Fixed"; "Desired Salary")' in lines[0]
    assert "- a Real Co — Engineer: drafted answers wait in its folder" in "\n".join(out)
    assert "- **a Real Co — Engineer**" in out


def test_a_night_with_nothing_new_to_score_says_so():
    df = _scored([(SINCE - timedelta(days=1), "score-judge:x", "tailor")])
    assert M.scoring_section(df, SINCE, "keepers=75", 0, 0)[0] == "Nothing new to score. Keepers 75."


def test_an_empty_night_still_writes_a_summary(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    monkeypatch.setattr(M.sheet, "rows", lambda: [])
    assert M.main(["--since", "2026-09-28T15:45:00"]) == 0
    text = M.OUT.read_text(encoding="utf-8")
    assert "Stages: discover not run" in text and "Nothing scored." in text
    assert "Nothing to tailor." in text


def _dupe(a, b, reason="other_name", date="2026-09-29", score=99.0):
    return {"pair": "|".join(sorted((a, b))), "job_id": a, "other": b, "company": "sogeti",
            "other_company": "capgemini", "title": "ai engineer", "jd_score": score,
            "reason": reason, "date": date}


def test_duplicates_lists_tonights_pairs_tracked_first_never_the_backfill():
    rows = [_dupe("aaaa0001", "bbbb0001"), _dupe("cccc0001", "dddd0001", score=91),
            _dupe("eeee0001", "ffff0001", reason="backfill_merge")]
    out = M.duplicates_section(rows, {"dddd0001": {"state": "applied"}})
    assert out[0] == "2 pairs not merged:"
    assert out[1].startswith("- `cccc0001` sogeti / `dddd0001` capgemini (applied) — ai engineer")
    assert "eeee0001" not in "\n".join(out)
    assert M.duplicates_section([], {}) == ["None tonight."]


def test_the_summary_shows_the_duplicates_section_and_its_cue(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    monkeypatch.setattr(M.sheet, "rows", lambda: [])
    M.MAYBE_DUPES.write_text(json.dumps(_dupe("aaaa0001", "bbbb0001")) + "\n"
                             + json.dumps(_dupe("cccc0001", "dddd0001", date="2026-09-01")) + "\n",
                             encoding="utf-8")
    assert M.main(["--since", "2026-09-28T15:45:00"]) == 0
    text = M.OUT.read_text(encoding="utf-8")
    assert "## Possible duplicates\n1 pair not merged:" in text and M.CUES["dupes"] in text
    assert "cccc0001" not in text
