"""The night runner: one lock, --resume picks the stages not ok, run.json replaced whole."""

from __future__ import annotations

import fcntl
import json

import pytest

from src import night as M

FAKE = '#!/bin/bash\necho "$(basename "$0" .sh)" >> "$RUN_DIR/../ran.txt"\nexit {rc}\n'


@pytest.fixture
def logs(tmp_path, monkeypatch):
    stages = tmp_path / "stages"
    stages.mkdir()
    for name in M.STAGES + M.FINISH:
        script = stages / f"{name}.sh"
        script.write_text(FAKE.format(rc=0), encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("STAGES_DIR", str(stages))
    monkeypatch.delenv("SKIP_STAGES", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    return tmp_path / "logs"


def _ran(logs):
    return (logs / "ran.txt").read_text(encoding="utf-8").split()


def test_a_second_runner_is_refused_while_the_lock_is_held(logs):
    logs.mkdir()
    with (logs / "night.lock").open("a+", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert M.main(["--dry-run"]) == M.LOCKED
    assert not list(logs.glob("pipeline_*"))


def test_resume_reruns_only_the_stages_not_ok_in_the_last_run_of_its_kind(logs):
    def night(stamp, dry, statuses):
        d = logs / f"pipeline_{stamp}"
        d.mkdir(parents=True)
        stages = {s: {"stage": s, "status": st, "rc": None} for s, st in statuses.items()}
        M.write_json(d / "run.json", {"stamp": stamp, "dry_run": dry, "stages": stages})
        return d

    target = night(
        "2026-10-01_200000",
        True,
        {
            "discover": "ok",
            "score": "ok",
            "synonyms": "fail",
            "tailor": "partial",
            "apply_prep": "deferred",
            "cleanup": "skipped",
        },
    )
    night("2026-10-02_200000", False, dict.fromkeys(M.STAGES, "fail"))  # real: not this kind
    assert M.main(["--dry-run", "--resume"]) == 0
    assert _ran(logs) == ["synonyms", "tailor", "apply_prep", "summary", "email"]
    run = json.loads((target / "run.json").read_text(encoding="utf-8"))
    assert run["stages"]["discover"]["status"] == "ok" and run["stages"]["tailor"]["rc"] == 0
    assert run["resumes"]


def test_a_failed_score_defers_the_rest_and_the_summary_still_runs(logs, tmp_path):
    (tmp_path / "stages" / "score.sh").write_text(FAKE.format(rc=1), encoding="utf-8")
    assert M.main(["--dry-run"]) == 1
    assert _ran(logs) == ["discover", "score", "summary", "email"]
    (d,) = logs.glob("pipeline_*")
    stages = json.loads((d / "run.json").read_text(encoding="utf-8"))["stages"]
    assert stages["score"]["status"] == "fail" and stages["score"]["rc"] == 1
    assert stages["tailor"]["status"] == "deferred"


def test_run_json_is_replaced_whole_or_not_at_all(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    M.write_json(path, {"stages": {"score": {"status": "ok"}}})

    def broken(*a, **k):
        a[1].write('{"stages": ')
        raise OSError("disk full")

    monkeypatch.setattr(M.json, "dump", broken)
    with pytest.raises(OSError):
        M.write_json(path, {"stages": {}})
    assert json.loads(path.read_text(encoding="utf-8"))["stages"]["score"]["status"] == "ok"
