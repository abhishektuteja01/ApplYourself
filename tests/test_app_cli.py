"""Tests for src/app_cli.py — `uv run app`, the plumbing of /tailor,
/cover-letter and /company-answers. Sources are the committed example world
(conftest), the verticals config is the fixture, Word is never called."""

from __future__ import annotations

import json
import re
import shutil

import pandas as pd
import pytest
import yaml

from src import app_cli
from src.session_schema import ProblemCode
from src.shared import paths
from tests.factories import scored_rows, write_clean

JOB = "a1b2c3d4"
JD = "Build widget assembly tooling in Python and SQL across 4 plants. Salary 120000."


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.setitem(paths._overrides, "CLEAN", tmp_path / "clean.parquet")
    monkeypatch.setitem(paths._overrides, "SCORED", tmp_path / "scored.parquet")
    (tmp_path / "profile").mkdir()
    shutil.copy(
        paths.example("resume_template.example.docx"),
        tmp_path / "profile" / "resume_template.docx",
    )
    shutil.copy(
        paths.example("cover_letter_template.example.docx"),
        tmp_path / "profile" / "cover_letter_template.docx",
    )
    monkeypatch.setattr(
        app_cli,
        "export_pdf",
        lambda docx, token: {"status": "ok", "path": "x", "pages": 1, "detail": ""},
    )
    _parquets(tmp_path)
    return tmp_path


def _parquets(tmp_path, **over):
    row = {
        "job_id": JOB,
        "company": "Acme Widgets",
        "title": "Widget Engineer",
        "url": "https://example.com/j",
        "posted_date": pd.NaT,
        "jd_text": JD,
        "vertical": "example_primary",
        "source": "greenhouse",
        "location": "Remote",
        **over,
    }
    write_clean(tmp_path / "clean.parquet", row)
    scored_rows(
        {
            "job_id": JOB,
            "vertical": row["vertical"],
            "fit_score": 80,
            "sponsorship_label": "unknown",
            "keywords_to_mirror": ["SQL", "Kubernetes"],
        }
    ).to_parquet(tmp_path / "scored.parquet")


def _run(capsys, *argv):
    code = app_cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _write(repo, rel_dir, name, data):
    (repo / rel_dir / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _baseline_plan():
    return {"entries": [{"id": b} for b in ("B-WID-01", "B-WID-02", "B-WID-03", "B-SPR-01")]}


def _built(repo, capsys):
    code, out = _run(capsys, "new", JOB, "--today", "2026-09-28")
    assert code == 0, out
    _write(repo, out["work_dir"], "resume_plan.yaml", _baseline_plan())
    code, built = _run(capsys, "build", "resume", JOB)
    assert code == 0, built
    return built["dir"]


# ---------- new ----------


def test_new_opens_a_folder_registers_the_role_and_snapshots_the_jd(repo, capsys):
    code, out = _run(capsys, "new", JOB, "--today", "2026-09-28")
    assert code == 0
    assert (
        out["work_dir"]
        == f"applications/example_primary/2026-09-28_acme-widgets_widget-engineer_{JOB}"
    )
    assert out["keywords_to_mirror"] == ["SQL", "Kubernetes"]
    head, _, body = (
        (repo / out["work_dir"] / "jd_snapshot.md")
        .read_text(encoding="utf-8")[4:]
        .partition("\n---\n")
    )
    facts = yaml.safe_load(head)
    assert facts["keywords_to_mirror"] == ["SQL", "Kubernetes"] and facts["posted_date"] == ""
    assert JD in body
    assert (
        yaml.safe_load((repo / "pipeline" / JOB / "state.yaml").read_text(encoding="utf-8"))[
            "state"
        ]
        == "saved"
    )


def test_new_reuses_an_unfinished_folder_instead_of_piling_up_versions(repo, capsys):
    _, first = _run(capsys, "new", JOB, "--today", "2026-09-28")
    _, again = _run(capsys, "new", JOB, "--today", "2026-09-29")
    assert again["work_dir"] == first["work_dir"]


def test_new_reuses_a_built_but_unrecorded_folder(repo, capsys, monkeypatch):
    monkeypatch.setattr(
        app_cli,
        "export_pdf",
        lambda docx, token: {"status": "ok", "path": "x", "pages": 2, "detail": ""},
    )
    _, first = _run(capsys, "new", JOB, "--today", "2026-09-28")
    _write(repo, first["work_dir"], "resume_plan.yaml", _baseline_plan())
    assert _run(capsys, "build", "resume", JOB)[0] == 2  # 2 pages: docx left, not recorded
    _, again = _run(capsys, "new", JOB, "--today", "2026-09-29")
    assert again["work_dir"] == first["work_dir"]


def test_new_after_a_built_resume_opens_the_next_version_across_lanes(repo, capsys):
    _built(repo, capsys)
    (repo / "applications" / "example_secondary" / f"2026-01-01_x_y_{JOB}_v4").mkdir(parents=True)
    _, out = _run(capsys, "new", JOB, "--today", "2026-09-30")
    assert out["work_dir"].endswith(f"{JOB}_v5")


def test_new_refuses_a_row_with_no_configured_lane(repo, capsys):
    _parquets(repo, vertical="")
    code, out = _run(capsys, "new", JOB)
    assert code == 1 and "no resume: block" in out["error"]


def test_new_slug_falls_back_to_the_job_id(repo, capsys):
    _parquets(repo, company="!!!", title="Ingénieur Widgets")
    _, out = _run(capsys, "new", JOB, "--today", "2026-09-28")
    assert out["work_dir"].endswith(f"2026-09-28_{JOB}_ingénieur-widgets_{JOB}")


def test_new_without_scored_parquet_errors(repo, capsys):
    (repo / "scored.parquet").unlink()
    code, out = _run(capsys, "new", JOB)
    assert code == 1 and "scored.parquet" in out["error"]


# ---------- build resume ----------


def test_build_resume_writes_docx_trace_and_state(repo, capsys):
    rel = _built(repo, capsys)
    folder = repo / rel
    assert (folder / "Alex_Example_Resume.docx").is_file()
    trace = (folder / "trace.md").read_text(encoding="utf-8")
    assert "source=B-WID-01 transformation=unchanged" in trace
    assert "- Kubernetes: not landed" in trace
    state = yaml.safe_load((repo / "pipeline" / JOB / "state.yaml").read_text(encoding="utf-8"))
    assert state["tailored_dirs"] == [rel.removeprefix("applications/")]
    assert state["state"] == "saved"  # building never transitions


def test_build_resume_reports_problems_and_writes_nothing(repo, capsys):
    _, out = _run(capsys, "new", JOB)
    plan = _baseline_plan()
    plan["entries"][0]["text"] = "Spearheaded widget work across 9 plants."
    _write(repo, out["work_dir"], "resume_plan.yaml", plan)
    code, res = _run(capsys, "build", "resume", JOB)
    assert code == 2
    assert {"license", "number", "lint"} <= {p["code"] for p in res["problems"]}
    assert not list((repo / out["work_dir"]).glob("*.docx"))

    code, res = _run(capsys, "build", "resume", JOB, "--final")
    assert code == 0 and res["swapped"] == ["B-WID-01 -> canonical"]


def test_build_resume_names_a_mistyped_key_instead_of_shipping_the_baseline(repo, capsys):
    _, out = _run(capsys, "new", JOB)
    plan = {**_baseline_plan(), "summry": {"text": "x"}}
    plan["entries"][1]["txt"] = "reworded"
    _write(repo, out["work_dir"], "resume_plan.yaml", plan)
    code, res = _run(capsys, "build", "resume", JOB)
    assert code == 2
    assert {(p["where"], p["detail"]) for p in res["problems"]} == {
        ("resume_plan.yaml: summry", "unknown key; closest valid key: 'summary'"),
        ("resume_plan.yaml: entries[1].txt", "unknown key; closest valid key: 'text'"),
    }


def test_no_fab_lists_exactly_the_problem_codes():
    text = (paths.REPO_ROOT / ".claude" / "shared" / "no_fab.md").read_text(encoding="utf-8")
    rows = [ln.split("|")[1] for ln in text.splitlines() if ln.startswith("| `")]
    assert set(re.findall(r"`(\w+)`", " ".join(rows))) == set(ProblemCode)


def test_build_resume_over_one_page_is_a_problem(repo, capsys, monkeypatch):
    monkeypatch.setattr(
        app_cli,
        "export_pdf",
        lambda docx, token: {"status": "ok", "path": "x", "pages": 2, "detail": ""},
    )
    _, out = _run(capsys, "new", JOB)
    _write(repo, out["work_dir"], "resume_plan.yaml", _baseline_plan())
    code, res = _run(capsys, "build", "resume", JOB)
    assert code == 2 and res["problems"][0]["code"] == "pages"
    assert (
        "tailored_dirs"
        not in yaml.safe_load((repo / "pipeline" / JOB / "state.yaml").read_text(encoding="utf-8"))
        or not yaml.safe_load((repo / "pipeline" / JOB / "state.yaml").read_text(encoding="utf-8"))[
            "tailored_dirs"
        ]
    )


def test_build_resume_without_a_pdf_fails_closed_unless_no_pdf(repo, capsys, monkeypatch):
    monkeypatch.setattr(
        app_cli,
        "export_pdf",
        lambda docx, token: {"status": "failed", "path": "", "pages": 0, "detail": "-1728"},
    )
    _, out = _run(capsys, "new", JOB)
    _write(repo, out["work_dir"], "resume_plan.yaml", _baseline_plan())
    code, res = _run(capsys, "build", "resume", JOB)
    assert code == 2 and res["problems"][0]["code"] == "pages"
    assert not (
        yaml.safe_load((repo / "pipeline" / JOB / "state.yaml").read_text(encoding="utf-8")) or {}
    ).get("tailored_dirs")
    code, res = _run(capsys, "build", "resume", JOB, "--no-pdf")
    assert code == 0, res


def test_no_pdf_never_starts_word_and_drops_an_older_pdf(repo, capsys, monkeypatch):
    rel = _built(repo, capsys)
    stale = repo / rel / "Alex_Example_Resume.pdf"
    stale.write_bytes(b"old")
    monkeypatch.setattr(app_cli, "export_pdf", lambda docx, token: pytest.fail("Word started"))
    code, res = _run(capsys, "build", "resume", JOB, "--no-pdf")
    assert code == 0 and res["pdf"]["status"] == "skipped" and not stale.exists()


# ---------- dir ----------


def test_dir_points_everything_at_the_built_folder(repo, capsys):
    rel = _built(repo, capsys)
    _, info = _run(capsys, "dir", JOB)
    assert (
        info["dir"] == rel
        and info["has_resume"]
        and not info["has_letter"]
        and not info["has_answers"]
    )
    _run(capsys, "new", JOB)  # a newer unfinished folder
    _, info = _run(capsys, "dir", JOB)
    assert info["dir"] == rel and info["work_dir"] != rel


# ---------- answers ----------


def _answers(**over):
    return {
        "researched_at": "2026-09-28",
        "sources": ["https://acme.example"],
        "uses": [],
        "why_company": "Acme builds widget tooling for plants. Its teams publish their line data.",
        "why_role": "The role builds assembly tooling in Python. It works across 4 plants.",
        "what_interests_you_about_product": None,
        **over,
    }


def test_answers_accepts_null_for_no_research(repo, capsys):
    rel = _built(repo, capsys)
    _write(repo, rel, "company_answers.yaml", _answers())
    code, out = _run(capsys, "build", "answers", JOB)
    assert code == 0 and out["no_research"] == ["what_interests_you_about_product"]


@pytest.mark.parametrize(
    "over,code",
    [
        ({"why_company": "One sentence only."}, "layout"),
        ({"why_role": "The role pays well. It cut costs 40% in a year."}, "number"),
        ({"why_company": "Acme is a robust team. It builds widgets."}, "lint"),
        ({"extra": "x"}, "layout"),
        ({"uses": ["B-NOPE-01"]}, "unknown_id"),
    ],
)
def test_answers_problems(repo, capsys, over, code):
    rel = _built(repo, capsys)
    _write(repo, rel, "company_answers.yaml", _answers(**over))
    rc, out = _run(capsys, "build", "answers", JOB)
    assert rc == 2 and code in {p["code"] for p in out["problems"]}


def test_answers_numbers_from_a_cited_bullet_pass(repo, capsys):
    rel = _built(repo, capsys)
    _write(
        repo,
        rel,
        "company_answers.yaml",
        _answers(
            uses=["B-SPR-01"],
            why_role="I scored 12,000 measurements before. This role does that daily.",
        ),
    )
    assert _run(capsys, "build", "answers", JOB)[0] == 0


# ---------- letter ----------


def _letter(**over):
    return {
        "salutation": "Dear May Chen,",
        "uses": ["B-WID-01"],
        "body": [
            "Acme builds the widget tooling I used across four plants.",
            "I owned the daily widget assembly throughput report for a national client.",
        ],
        **over,
    }


def test_letter_renders_and_never_transitions(repo, capsys):
    rel = _built(repo, capsys)
    _write(repo, rel, "cover_letter.yaml", _letter())
    code, out = _run(capsys, "build", "letter", JOB)
    assert code == 0, out
    assert (repo / rel / "Alex_Example_Cover_Letter.docx").is_file()
    assert (
        yaml.safe_load((repo / "pipeline" / JOB / "state.yaml").read_text(encoding="utf-8"))[
            "state"
        ]
        == "saved"
    )


@pytest.mark.parametrize(
    "over,code",
    [
        ({"body": "one string"}, "layout"),
        ({"body": ["We share a love of widgets across 97 plants at Acme."]}, "number"),
        ({"body": ["I love widgets and want this job."]}, "layout"),  # never names the company
        ({"uses": []}, "layout"),
    ],
)
def test_letter_problems(repo, capsys, over, code):
    rel = _built(repo, capsys)
    _write(repo, rel, "cover_letter.yaml", _letter(**over))
    rc, out = _run(capsys, "build", "letter", JOB)
    assert rc == 2 and code in {p["code"] for p in out["problems"]}


def test_letter_before_a_resume_is_refused(repo, capsys):
    _run(capsys, "new", JOB)
    code, out = _run(capsys, "build", "letter", JOB)
    assert code == 1 and "no built resume" in out["error"]


def _dupes(repo, monkeypatch, rows, states=(), sent=()):
    monkeypatch.setitem(paths._overrides, "MAYBE_DUPES", repo / "maybe_duplicates.jsonl")
    monkeypatch.setitem(paths._overrides, "APPLY_RUNS", repo)
    (repo / "maybe_duplicates.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    for jid, state in states:
        (repo / "pipeline" / jid).mkdir(parents=True)
        _write(
            repo,
            f"pipeline/{jid}",
            "state.yaml",
            {"job_id": jid, "company": "Vouch Insurance", "title": "AI Engineer", "state": state},
        )
    if sent:
        (repo / "submitted.jsonl").write_text(
            "".join(json.dumps({"job_id": j, "status": "submitted"}) + "\n" for j in sent),
            encoding="utf-8",
        )


def _maybe(a, b, score=99.4):
    return {
        "pair": "|".join(sorted((a, b))),
        "job_id": a,
        "other": b,
        "company": "vouch",
        "other_company": "vouch insurance",
        "title": "ai engineer",
        "jd_score": score,
        "reason": "backfill_merge",
    }


def test_dupes_names_a_partner_already_applied_or_submitted(repo, capsys, monkeypatch):
    _dupes(
        repo,
        monkeypatch,
        [_maybe(JOB, "2add76fe"), _maybe("c0ffee00", JOB, 92)],
        states=[("2add76fe", "applied")],
        sent=["c0ffee00"],
    )
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))
    code, out = _run(capsys, "dupes", JOB)
    assert code == 0 and sorted(p.relative_to(repo) for p in repo.rglob("*")) == before
    assert [(d["job_id"], d["state"], d["submitted"]) for d in out["duplicates"]] == [
        ("2add76fe", "applied", False),
        ("c0ffee00", "", True),
    ]
    assert out["duplicates"][1]["company"] == "vouch"


def test_dupes_stays_quiet_for_idle_partners_and_other_roles(repo, capsys, monkeypatch):
    _dupes(
        repo,
        monkeypatch,
        [_maybe(JOB, "2add76fe"), _maybe("11111111", "22222222")],
        states=[("2add76fe", "saved")],
    )
    assert _run(capsys, "dupes", JOB) == (0, {"job_id": JOB, "duplicates": []})


def test_lint_outreach_on_a_missing_draft_prints_one_line(repo, capsys):
    code = app_cli.main(["lint", "outreach", str(repo / "absent.md"), "--company", "Acme"])
    out = capsys.readouterr().out
    assert code == 2 and out.count("\n") == 1 and "absent.md" in json.loads(out)["error"]


# ---------- outreach ----------


def _outreach(repo, capsys, limits=None, **over):
    rel = _built(repo, capsys)
    auto = yaml.safe_load(paths.example("settings.example.yaml").read_text(encoding="utf-8"))
    auto["outreach_word_limits"].update(limits or {})
    (repo / "profile" / "settings.yaml").write_text(yaml.safe_dump(auto), encoding="utf-8")
    draft = {
        "channel": "recruiter",
        "medium": "email",
        "to": "May Chen",
        "subject": "Widget Engineer role",
        "uses": ["B-WID-01"],
        "body": "I owned the daily widget assembly throughput report across four plants.",
        "why": "Short and direct, as the voice samples are.",
        **over,
    }
    _write(repo, rel, "outreach.yaml", draft)
    return _run(capsys, "build", "outreach", JOB)


def test_outreach_writes_the_draft_and_records_it_once(repo, capsys):
    code, out = _outreach(repo, capsys)
    assert code == 0, out
    assert _run(capsys, "build", "outreach", JOB)[0] == 0
    text = (repo / out["draft"]).read_text(encoding="utf-8")
    assert "# Subject" in text and "# Why this approach" in text
    state = yaml.safe_load((repo / "pipeline" / JOB / "state.yaml").read_text(encoding="utf-8"))
    assert [(e["draft_file"], e["medium"]) for e in state["outreach"]] == [
        (out["draft_file"], "email")
    ]


def test_outreach_length_comes_from_the_profile_limits(repo, capsys):
    code, out = _outreach(repo, capsys, limits={"recruiter_email": 5})
    assert code == 2 and [p["code"] for p in out["problems"]] == ["length"]
    assert not (repo / "pipeline" / JOB / "outreach").exists()
