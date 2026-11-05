"""One offline night, end to end: a fake lane's postings -> the jobs store ->
clean.parquet -> score start / recorded judge answer / finish -> app new +
build resume (docx only) -> track. No network, no LLM, no Word.

The location allowlist is stubbed: the real one needs libpostal."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from src import app_cli, score_store, track_cli
from src.discovery import live_view
from src.discovery import store as S
from src.score_filter import SCORING_PATH, CountryRule, load_scoring
from src.shared import paths
from src.shared.location.allowlist import Allowed

SESSIONS = Path(__file__).resolve().parent.parent / "fixtures" / "sessions"
JD = (SESSIONS / "jd.txt").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 27, 12, 0)
TODAY = pd.Timestamp(NOW.date())
KEPT = "b1ccd805"  # hash of Acme Widgets | Widget Operations Analyst


class _AllowAll:
    def check(self, raw, vertical="", title=""):
        return Allowed(True, raw, True)


def _lane_postings() -> list[dict]:
    """Three postings: one kept, one the title gate drops, one the score filter drops."""
    gh = "https://job-boards.greenhouse.io/{}/jobs/{}"
    common = {
        "source": "greenhouse",
        "vertical": "example_primary",
        "location_raw": "Exampletown, ST",
        "employment_type": "fulltime",
        "posted_date": TODAY - pd.Timedelta(days=2),
    }
    return [
        S.make_posting(
            **common,
            company="Acme Widgets",
            title="Widget Operations Analyst",
            url=gh.format("acmewidgets", 1),
            jd_text=JD,
        ),
        S.make_posting(
            **common,
            company="Acme Widgets",
            title="Senior Widget Analyst",
            url=gh.format("acmewidgets", 2),
            jd_text=JD,
        ),
        S.make_posting(
            **common,
            company="Beta Gizmos",
            title="Gizmo Reconciliation Analyst",
            url=gh.format("betagizmos", 3),
            jd_text=JD + " You will migrate us off the legacy gizmo stack.",
        ),
    ]


def _app(capsys, *argv) -> dict:
    code = app_cli.main(list(argv))
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    return out


def test_one_night_from_lane_to_tracked_role(tmp_path, monkeypatch, capsys):
    clean, scored, staging = (
        live_view.CLEAN,
        tmp_path / "jobs" / "scored.parquet",
        tmp_path / "staging",
    )
    for mod in (app_cli, track_cli):
        monkeypatch.setattr(mod, "CLEAN", clean)
        monkeypatch.setattr(mod, "SCORED", scored)
    monkeypatch.setattr(track_cli, "STORE", S.STORE)
    monkeypatch.setattr(app_cli, "PROFILE", tmp_path / "profile")
    monkeypatch.setattr(
        app_cli,
        "export_pdf",
        lambda docx, token: {"status": "skipped", "path": "", "pages": 0, "detail": ""},
    )
    (tmp_path / "profile").mkdir()
    shutil.copy(
        paths.PROFILE / "resume_template.example.docx",
        tmp_path / "profile" / "resume_template.docx",
    )

    # discovery: lane -> store -> live view
    batch = S.Batch()
    for row in _lane_postings():
        batch.upsert(row)
    stats = S.flush(batch, TODAY, frozenset(), live_days=30)
    assert stats.inserted == 3
    inp = live_view.ViewInputs(
        today=TODAY,
        state_index={},
        scores={},
        live_days=30,
        search_max_age_days=30,
        score_threshold=60,
        high_days=60,
        low_days=14,
    )
    view, funnel = live_view.build_view(S.load_store(), _AllowAll(), inp)
    live_view.write_view(view)
    assert funnel["after_title_gate"] == 2
    on_disk = pd.read_parquet(clean)
    assert list(on_disk.columns) == live_view.CLEAN_COLUMNS
    assert KEPT in set(on_disk["job_id"]) and len(on_disk) == 2

    # score: filter drop, then the recorded judge answer
    scfg = load_scoring(SCORING_PATH.with_name("scoring.example.yaml"))
    started = score_store.start(clean, scored, staging, scfg, now=NOW)
    assert started["dropped"] == {"phrase": 1} and started["judge"] == 1
    (name,) = score_store.load_run(staging)["slices"]
    shutil.copy(SESSIONS / "judge_answer.json", staging / f"out_{name}.json")
    done = score_store.finish(
        clean,
        scored,
        staging,
        paths.PIPELINE,
        scfg,
        CountryRule(home=frozenset({"us"}), abroad={}),
        now=NOW,
    )
    assert done["merged"] == 1 and done["bad"] == {}
    row = pd.read_parquet(scored).set_index("job_id").loc[KEPT]
    assert row["fit_score"] == 86 and row["suggested_action"] == "tailor"
    assert row["sponsorship_label"] != "ineligible"

    # material: app new + a recorded plan -> docx
    work = _app(capsys, "new", KEPT, "--today", "2026-09-28")["work_dir"]
    shutil.copy(SESSIONS / "resume_plan.yaml", tmp_path / work)
    built = _app(capsys, "build", "resume", KEPT, "--no-pdf")
    assert (tmp_path / built["dir"] / built["docx"]).is_file()

    # track: the one writer of state
    assert track_cli.main([KEPT, "tailored"]) == 0
    state = yaml.safe_load((paths.PIPELINE / KEPT / "state.yaml").read_text(encoding="utf-8"))
    assert state["state"] == "tailored" and state["company"] == "Acme Widgets"
    assert [h["state"] for h in state["state_history"]] == ["saved", "tailored"]
    assert state["tailored_dirs"] == [built["dir"].removeprefix("applications/")]
