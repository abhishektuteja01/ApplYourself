"""Recorded session output (tests/fixtures/sessions/) through its builder or
validator. Refresh a fixture when its command .md changes the output shape."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from src import app_cli, score_store
from src.material import build as material_build
from src.score_filter import load_scoring
from src.shared import paths
from tests.factories import scored_rows, write_clean

SESSIONS = Path(__file__).resolve().parents[1] / "fixtures" / "sessions"
JD = (SESSIONS / "jd.txt").read_text(encoding="utf-8")
ROLE = {
    "company": "Acme Widgets",
    "title": "Widget Operations Analyst",
    "source": "greenhouse",
    "url": "https://job-boards.greenhouse.io/acmewidgets/jobs/1",
    "location": "Exampletown, ST",
    "vertical": "example_primary",
    "employment_type": "fulltime",
    "jd_text": JD,
}
JOB = "b1ccd805"  # the hash of ROLE's company + title


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(paths._overrides, "CLEAN", tmp_path / "clean.parquet")
    monkeypatch.setitem(paths._overrides, "SCORED", tmp_path / "scored.parquet")
    monkeypatch.setattr(
        material_build,
        "export_pdf",
        lambda docx, token: {"status": "skipped", "path": "", "pages": 0, "detail": ""},
    )
    (tmp_path / "profile").mkdir()
    for kind in ("resume", "cover_letter"):
        shutil.copy(
            paths.example(f"{kind}_template.example.docx"),
            tmp_path / "profile" / f"{kind}_template.docx",
        )
    assert write_clean(tmp_path / "clean.parquet", ROLE)["job_id"][0] == JOB
    return tmp_path


def _app(*argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = app_cli.main(list(argv))
    return code, json.loads(buf.getvalue())


def _open_folder(world) -> Path:
    keywords = json.loads((SESSIONS / "judge_answer.json").read_text(encoding="utf-8"))[0]
    scored_rows(
        {
            "job_id": JOB,
            "vertical": "example_primary",
            "fit_score": 86.0,
            "keywords_to_mirror": keywords["keywords_to_mirror"],
        }
    ).to_parquet(world / "scored.parquet")
    code, out = _app("new", JOB, "--today", "2026-09-28")
    assert code == 0, out
    return world / out["work_dir"]


def _build_resume(world) -> Path:
    folder = _open_folder(world)
    shutil.copy(SESSIONS / "resume_plan.yaml", folder)
    code, out = _app("build", "resume", JOB, "--no-pdf")
    assert code == 0, out
    return folder


def _judge(world) -> list[str]:
    staging = world / "staging"
    score_store.start(
        world / "clean.parquet",
        world / "scored.parquet",
        staging,
        load_scoring(paths.example("scoring.example.yaml")),
        now=datetime(2026, 9, 27, 12, 0),
    )
    (meta,) = score_store.load_run(staging)["slices"].values()
    answer = json.loads((SESSIONS / "judge_answer.json").read_text(encoding="utf-8"))
    return score_store.check_answer(answer, meta)


def _built(what: str, fixture: str):
    def run(world):
        folder = _build_resume(world)
        shutil.copy(SESSIONS / fixture, folder)
        shutil.copy(paths.example("settings.example.yaml"), world / "profile" / "settings.yaml")
        code, out = (
            _app("build", what, JOB, "--no-pdf") if what == "letter" else _app("build", what, JOB)
        )
        return [] if code == 0 else out.get("problems", out)

    return run


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(lambda w: [] if _build_resume(w) else ["no folder"], id="resume_plan"),
        pytest.param(_built("letter", "cover_letter.yaml"), id="cover_letter"),
        pytest.param(_built("answers", "company_answers.yaml"), id="company_answers"),
        pytest.param(_built("outreach", "outreach.yaml"), id="outreach"),
        pytest.param(_judge, id="judge_answer"),
    ],
)
def test_recorded_session_output_passes_its_builder(world, check):
    assert check(world) == []
