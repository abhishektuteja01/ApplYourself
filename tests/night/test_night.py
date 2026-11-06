"""The night runner: one lock, --resume picks the stages not ok, run.json replaced whole."""

from __future__ import annotations

import fcntl
import json

import pytest

from applyourself import night as M

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

    from applyourself.shared import files

    def broken(fd):
        raise OSError("disk full")  # the temp file holds the new text, not yet renamed

    monkeypatch.setattr(files.os, "fsync", broken)
    with pytest.raises(OSError):
        M.write_json(path, {"stages": {}})
    assert json.loads(path.read_text(encoding="utf-8"))["stages"]["score"]["status"] == "ok"
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]


def test_a_run_json_from_newer_code_is_refused(tmp_path):
    from applyourself.shared import schemas

    (tmp_path / "run.json").write_text(
        json.dumps({"schema_version": schemas.NIGHT_RUN_SCHEMA_VERSION + 1, "stages": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema version"):
        M.load(tmp_path)


def test_the_spend_cap_defers_the_claude_stages_left_and_the_finishers_still_run(logs, tmp_path):
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "settings.yaml").write_text(
        "max_spend_usd_per_night: 5\n", encoding="utf-8"
    )
    (tmp_path / "stages" / "score.sh").write_text(
        '#!/bin/bash\necho "score" >> "$RUN_DIR/../ran.txt"\n'
        'printf "judge_1 3.5\\njudge_2 1.5\\n" >> "$RUN_DIR/score.cost"\n',
        encoding="utf-8",
    )
    assert M.main(["--dry-run"]) == 0
    assert _ran(logs) == ["discover", "score", "cleanup", "summary", "email"]
    (d,) = logs.glob("pipeline_*")
    run = json.loads((d / "run.json").read_text(encoding="utf-8"))
    assert run["cost_usd"] == 5.0 and run["stages"]["score"]["cost_usd"] == 5.0
    for name in ("synonyms", "tailor", "apply_prep"):
        assert run["stages"][name]["status"] == "deferred"
        assert run["stages"][name]["detail"] == M.SPEND_CAP


def test_run_claude_logs_every_calls_cost_even_a_retry_that_reuses_the_result_file(tmp_path):
    import subprocess
    import sys

    from applyourself.shared import paths

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(
        '#!/bin/bash\necho \'{"is_error": false, "total_cost_usd": 0.25}\'\n', encoding="utf-8"
    )
    (fake / "uv").write_text(  # `run config get` -> 10; `run python -` -> this python
        f'#!/bin/bash\n[[ "$2" == python ]] && {{ shift 2; exec "{sys.executable}" "$@"; }}\n'
        "echo 10\n",
        encoding="utf-8",
    )
    for f in fake.iterdir():
        f.chmod(0o755)
    lib = paths.REPO_ROOT / "scripts" / "lib_pipeline.sh"
    script = (
        f'source "{lib}"; STAGE=tailor; LOG=/dev/null; run_claude a1 Read p; run_claude a1 Read p'
    )
    env = {"PATH": "/usr/bin:/bin", "UV": str(fake / "uv"), "CLAUDE": str(fake / "claude")}
    env["RUN_DIR"] = str(tmp_path / "run")
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
    assert (tmp_path / "run" / "tailor.cost").read_text(encoding="utf-8") == "a1 0.25\na1 0.25\n"
    assert M._cost(tmp_path / "run" / "tailor.cost") == 0.5


def test_a_usage_limit_is_a_run_json_field_and_a_stale_mark_is_cleared(logs, tmp_path):
    (tmp_path / "stages" / "score.sh").write_text(
        '#!/bin/bash\necho score >> "$RUN_DIR/../ran.txt"\n: > "$RUN_DIR/score.quota"\nexit 1\n',
        encoding="utf-8",
    )
    assert M.main(["--dry-run"]) == 1
    (d,) = logs.glob("pipeline_*")
    assert json.loads((d / "run.json").read_text(encoding="utf-8"))["stages"]["score"][
        "usage_limit"
    ]
    (tmp_path / "stages" / "score.sh").write_text(FAKE.format(rc=0), encoding="utf-8")
    assert M.main(["--dry-run", "--resume"]) == 0
    stages = json.loads((d / "run.json").read_text(encoding="utf-8"))["stages"]
    assert stages["score"]["usage_limit"] is False and not (d / "score.quota").exists()


def test_reset_at_reads_the_limit_messages_claude_prints():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    la = ZoneInfo("America/Los_Angeles")

    def local(*a):
        return datetime(*a, tzinfo=la).astimezone().replace(tzinfo=None)

    now = local(2026, 11, 6, 23, 0)
    text = "You've hit your 5-hour limit · resets 12:10am (America/Los_Angeles)"
    assert M.reset_at(text, now) == local(2026, 11, 7, 0, 10)  # past midnight: tomorrow
    text = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"
    assert M.reset_at(text, now) == local(2026, 11, 6, 23, 50)
    assert M.reset_at(
        "You've hit your weekly limit · resets Nov 10, 5pm", datetime(2026, 11, 6)
    ) == (datetime(2026, 11, 10, 17, 0))
    assert M.reset_at("You've hit your limit", now) is None


@pytest.mark.parametrize("wait_until, waited", [("19:00", True), ("00:30", False)])
def test_a_usage_limit_waits_for_the_reset_then_reruns_the_stage(
    logs, tmp_path, monkeypatch, wait_until, waited
):
    from datetime import datetime

    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "settings.yaml").write_text(
        f'usage_limit_wait_until: "{wait_until}"\n', encoding="utf-8"
    )
    (tmp_path / "stages" / "score.sh").write_text(
        '#!/bin/bash\necho score >> "$RUN_DIR/../ran.txt"\n'
        '[[ -f "$RUN_DIR/../once" ]] && exit 0\n: > "$RUN_DIR/../once"\n'
        'echo "You\'ve hit your 5-hour limit · resets 1:10am" > "$RUN_DIR/score.quota"\nexit 1\n',
        encoding="utf-8",
    )
    slept = []
    monkeypatch.setattr(M.clock, "now", lambda: datetime(2026, 11, 6, 23, 0))
    monkeypatch.setattr(M, "_sleep_until", slept.append)
    monkeypatch.setattr(M.shutil, "which", lambda _: None)  # no caffeinate
    assert M.main([]) == (0 if waited else 1)
    (d,) = (p for p in logs.glob("pipeline_*") if p.is_dir())
    run = json.loads((d / "run.json").read_text(encoding="utf-8"))
    (wait,) = run["waits"]
    assert wait["stage"] == "score" and wait["until"] == "2026-11-07T01:13:00"
    assert wait["waited"] is waited
    if waited:
        assert slept == [datetime(2026, 11, 7, 1, 13)]
        assert _ran(logs)[:3] == ["discover", "score", "score"]
        assert run["stages"]["score"]["status"] == "ok" and run["stages"]["tailor"]["rc"] == 0
    else:
        assert slept == [] and _ran(logs) == ["discover", "score", "summary", "email"]
        assert run["stages"]["tailor"]["status"] == "deferred"
    history = (logs / "pipeline_history.log").read_text(encoding="utf-8")
    assert ("waited 1x for the usage limit" in history) is waited
