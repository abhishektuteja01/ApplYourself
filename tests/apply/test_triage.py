"""apply triage: which roles need a model and which can be filled as they are."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import apply_cli, track_cli
from src.apply import triage
from src.apply.greenhouse import PostingExpired
from src.apply.plan import FieldPlan, Plan, Unmapped
from src.shared import paths


def _field(fid="q1", source="bank:phone", value="+1"):
    return FieldPlan(
        id=fid,
        name=fid,
        label="Phone",
        kind="text",
        section="questions",
        required=True,
        multi=False,
        value=value,
        tier="A",
        source=source,
    )


def _unmapped(required=True, tier="B", blocks=False):
    return Unmapped(
        id="u1",
        label="Why?",
        required=required,
        kind="text",
        section="questions",
        tier=tier,
        reason="no answer",
        blocks=blocks,
    )


def _plan(job_id="aaaa1111", fields=(), unmapped=(), draftable=(), selector="#go"):
    return Plan(
        job_id=job_id,
        board="acme",
        token="1",
        form_url="https://x/1",
        company="Acme",
        title="Eng",
        out_dir=Path("/tmp"),
        fields=tuple(fields),
        files=(),
        unmapped=tuple(unmapped),
        draftable=tuple(draftable),
        skipped=(),
        submit_selector=selector,
        submit_disabled=False,
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    calls = {"track": [], "run": []}
    w = type("W", (), {"calls": calls, "tmp": tmp_path, "category": "ready"})
    monkeypatch.setattr(track_cli, "main", lambda argv: calls["track"].append(argv) or 0)
    monkeypatch.setattr(apply_cli, "resolve_out_dir", lambda job_id: tmp_path / job_id)
    plans: dict = {}
    w.plans = plans
    monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (plans[job_id], None))

    def run_role(job_id, **kw):
        calls["run"].append((job_id, kw))
        (tmp_path / job_id).mkdir(exist_ok=True)
        return apply_cli.RunOutcome(job_id, "Acme", "Eng", w.category)

    monkeypatch.setattr(apply_cli, "_run_role", run_role)
    return w


def test_a_role_with_every_answer_known_is_ready(world):
    world.plans["a"] = _plan("a", fields=[_field()])
    assert triage.classify("a") == {"job_id": "a", "kind": "ready", "detail": ""}


def test_an_optional_unmapped_field_does_not_hold_a_role_back(world):
    world.plans["a"] = _plan("a", fields=[_field()], unmapped=[_unmapped(required=False)])
    assert triage.classify("a")["kind"] == "ready"


@pytest.mark.parametrize(
    "plan,want",
    [
        (
            _plan(fields=[_field(source="bank:linkedin~linkedin")]),
            {"guesses": 1, "asks": 0, "facts": 0},
        ),
        (_plan(draftable=[_unmapped(tier="C")]), {"guesses": 0, "asks": 1, "facts": 0}),
        (_plan(unmapped=[_unmapped(tier="C")]), {"guesses": 0, "asks": 1, "facts": 0}),
        (_plan(unmapped=[_unmapped(tier="B")]), {"guesses": 0, "asks": 0, "facts": 1}),
        (
            _plan(unmapped=[_unmapped(required=False, blocks=True)]),
            {"guesses": 0, "asks": 0, "facts": 1},
        ),
        (_plan(selector=None), {"guesses": 0, "asks": 0, "facts": 0}),
    ],
)
def test_anything_new_guessed_or_missing_makes_a_role_open(world, plan, want):
    world.plans["aaaa1111"] = plan
    row = triage.classify("aaaa1111")
    assert row["kind"] == "open" and row["detail"] == want


def test_expired_manual_and_broken_forms_are_named_not_raised(world, monkeypatch):
    def build(job_id, **kw):
        raise {
            "e": PostingExpired("gone"),
            "m": apply_cli.ManualApplyOnly("workday"),
            "b": apply_cli.ApplyCliError("bad url"),
            "x": RuntimeError("boom"),
        }[job_id]

    monkeypatch.setattr(apply_cli, "build", build)
    kinds = {j: triage.classify(j) for j in "embx"}
    assert [kinds[j]["kind"] for j in "embx"] == ["expired", "manual", "error", "error"]
    assert "unexpected RuntimeError: boom" in kinds["x"]["detail"]


def test_without_fill_nothing_is_touched(world):
    world.plans["a"] = _plan("a", fields=[_field()])
    result = triage.run(["a"], fill=False, headless=True)
    assert result["ready"] == ["a"] and not world.calls["track"] and not world.calls["run"]
    assert json.loads(paths.APPLY_TRIAGE.read_text(encoding="utf-8"))["ready"] == ["a"]


def test_fill_tracks_then_fills_each_ready_role_and_never_submits(world):
    world.plans.update(
        {
            "a": _plan("a", fields=[_field()]),
            "b": _plan("b", fields=[_field(source="bank:x~x")]),
            "c": _plan("c", fields=[_field()]),
        }
    )
    result = triage.run(["a", "b", "c"], fill=True, headless=True)
    assert result["ready"] == ["a", "c"] and list(result["open"]) == ["b"]
    assert [c[0] for c in world.calls["track"]] == ["a", "c"]
    assert world.calls["track"][0][1:4] == ["tailored", "--if", "saved"]
    ran = {job: kw for job, kw in world.calls["run"]}
    assert set(ran) == {"a", "c"}
    assert all(kw["submit"] is False and kw["headless"] is True for kw in ran.values())
    assert ran["a"]["manifest_path"] == world.tmp / "a" / "form_manifest.json"
    assert Path(result["report"]).is_file()


def test_a_fill_that_fails_is_an_error_and_not_ready(world):
    world.plans["a"] = _plan("a", fields=[_field()])
    world.category = "failed"
    result = triage.run(["a"], fill=True, headless=True)
    assert result["ready"] == [] and result["errors"] == {"a": "failed: "}


def test_a_page_that_disagrees_after_the_fill_sends_the_role_to_a_session(world):
    world.plans["a"] = _plan("a", fields=[_field()])
    (world.tmp / "a").mkdir()
    (world.tmp / "a" / "form_manifest.json").write_text(
        json.dumps(
            {
                "fields": [
                    {"group": "filled", "matches": False},
                    {"group": "filled", "matches": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    result = triage.run(["a"], fill=True, headless=True)
    assert result["ready"] == [] and "1 field(s) the page disagrees with" in str(
        result["open"]["a"]
    )


def test_a_track_failure_stops_that_role_only(world, monkeypatch):
    world.plans.update({"a": _plan("a", fields=[_field()]), "b": _plan("b", fields=[_field()])})
    monkeypatch.setattr(track_cli, "main", lambda argv: 1 if argv[0] == "a" else 0)
    result = triage.run(["a", "b"], fill=True, headless=True)
    assert result["ready"] == ["b"] and result["errors"] == {"a": "track exited 1"}


def test_main_takes_the_resume_ready_roles_from_the_sheet_and_honours_limit(
    world, monkeypatch, capsys
):
    from src.apply import sheet

    monkeypatch.setattr(
        sheet, "job_ids", lambda status: ["a", "b", "c"] if status == "Resume ready" else []
    )
    world.plans.update({j: _plan(j, fields=[_field()]) for j in "abc"})
    assert triage.main(None, fill=False, headless=True, limit=2) == 0
    assert capsys.readouterr().out.startswith("ready 2 · open 0")
    assert triage.main(["c"], fill=False, headless=True, limit=None) == 0
