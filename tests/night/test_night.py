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


def test_run_claude_gives_the_call_no_stdin(tmp_path):
    # claude -p appends piped stdin to the prompt; a judge started inside a
    # `while read` loop used to get every other judge's slice line too.
    import subprocess
    import sys

    from applyourself.shared import paths

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(
        f'#!/bin/bash\nwc -c | tr -d " " > "{tmp_path}/stdin_bytes"\n'
        'echo \'{"is_error": false, "total_cost_usd": 0.1}\'\n',
        encoding="utf-8",
    )
    (fake / "uv").write_text(
        f'#!/bin/bash\n[[ "$2" == python ]] && {{ shift 2; exec "{sys.executable}" "$@"; }}\n'
        "echo 10\n",
        encoding="utf-8",
    )
    for f in fake.iterdir():
        f.chmod(0o755)
    lib = paths.REPO_ROOT / "scripts" / "lib_pipeline.sh"
    script = f'source "{lib}"; STAGE=score; LOG=/dev/null; run_claude j1 Read p'
    env = {"PATH": "/usr/bin:/bin", "UV": str(fake / "uv"), "CLAUDE": str(fake / "claude")}
    env["RUN_DIR"] = str(tmp_path / "run")
    subprocess.run(
        ["bash", "-c", script],
        env=env,
        input="judge other slices\n",
        text=True,
        encoding="utf-8",
        check=True,
        capture_output=True,
    )
    assert (tmp_path / "stdin_bytes").read_text(encoding="utf-8").strip() == "0"


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
        'echo "You\'ve hit your 5-hour limit · resets 1:10am" > "$RUN_DIR/score.quota"\n'
        'touch -t 202611062300 "$RUN_DIR/score.quota"\nexit 1\n',
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
        assert [p["status"] for p in run["stages"]["score"]["passes"]] == ["fail"]
    else:
        assert slept == [] and _ran(logs) == ["discover", "score", "summary", "email"]
        assert run["stages"]["tailor"]["status"] == "deferred"
    history = (logs / "pipeline_history.log").read_text(encoding="utf-8")
    assert ("waited 1x for the usage limit" in history) is waited


def test_a_night_fired_while_one_runs_exits_and_leaves_a_history_line(logs):
    logs.mkdir()
    with (logs / "night.lock").open("a+", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.write("pid 123 since 2026-11-06T21:00:00\n")
        held.flush()
        assert M.main([]) == M.LOCKED
    assert not [p for p in logs.glob("pipeline_*") if p.is_dir()]
    history = (logs / "pipeline_history.log").read_text(encoding="utf-8")
    assert history.endswith(
        "not started: another night holds night.lock (pid 123 since 2026-11-06T21:00:00)\n"
    )
    assert M.main(["--dry-run"]) == 0  # the lock is free again; the holder's note is replaced
    assert "pid 123" not in (logs / "night.lock").read_text(encoding="utf-8")


def test_a_rerun_after_a_wait_keeps_the_cap_and_skips_the_roles_done(tmp_path):
    import subprocess

    from applyourself.shared import paths

    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/bash\ncase "$*" in\n'
        '  "run config get settings tailor_parallel") echo 5;;\n'
        '  "run config get settings "*_max_per_night) echo 2;;\n'
        '  "run config get settings "*) echo 0;;\n'
        '  "run apply sheet --list "*) printf "aaaa\\nbbbb\\ncccc\\n";;\n'
        '  "run night tailor-pick "*) cat;;\n'
        "esac\n",
        encoding="utf-8",
    )
    one = tmp_path / "tailor_one.sh"
    one.write_text('#!/bin/bash\necho "$1 ok" >> "$RUN_DIR/tailor_results.txt"\n', encoding="utf-8")
    for f in (uv, one):
        f.chmod(0o755)
    run = tmp_path / "run"
    run.mkdir()
    (run / "tailor_results.txt").write_text("aaaa fail\nbbbb deferred\n", encoding="utf-8")
    (run / "apply_prep.tried").write_text("aaaa fail\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "UV": str(uv), "RUN_DIR": str(run)}
    env |= {
        "DRY_RUN": "1",
        "NIGHT_RERUN": "1",
        "STAGES_DIR": str(tmp_path),
        "PDF_BROKER_DIR": str(tmp_path),
    }
    for stage in ("tailor", "apply_prep"):
        script = paths.REPO_ROOT / "scripts" / "stages" / f"{stage}.sh"
        subprocess.run([str(script)], env=env, check=True, capture_output=True)
    # aaaa (failed) already took one slot of the cap of 2: only bbbb runs, cccc waits
    assert sorted((run / "tailor_results.txt").read_text(encoding="utf-8").splitlines()) == [
        "aaaa fail",
        "bbbb ok",
        "cccc deferred",
    ]
    assert (run / "apply_prep.tried").read_text(encoding="utf-8") == "aaaa fail\nbbbb ok\n"


def test_tailor_takes_every_new_role_when_the_pick_fails(tmp_path):
    import subprocess

    from applyourself.shared import paths

    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/bash\ncase "$*" in\n'
        '  "run config get settings tailor_parallel") echo 5;;\n'
        '  "run config get settings "*) echo 0;;\n'
        '  "run apply sheet --list "*) printf "aaaa\\nbbbb\\n";;\n'
        '  "run night tailor-pick "*) echo aaaa; exit 1;;\n'
        "esac\n",
        encoding="utf-8",
    )
    one = tmp_path / "tailor_one.sh"
    one.write_text('#!/bin/bash\necho "$1 ok" >> "$RUN_DIR/tailor_results.txt"\n', encoding="utf-8")
    for f in (uv, one):
        f.chmod(0o755)
    run = tmp_path / "run"
    run.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "UV": str(uv), "RUN_DIR": str(run)}
    env |= {"DRY_RUN": "1", "STAGES_DIR": str(tmp_path), "PDF_BROKER_DIR": str(tmp_path)}
    script = paths.REPO_ROOT / "scripts" / "stages" / "tailor.sh"
    subprocess.run([str(script)], env=env, check=True, capture_output=True)
    assert sorted((run / "tailor_results.txt").read_text(encoding="utf-8").splitlines()) == [
        "aaaa ok",
        "bbbb ok",
    ]


def test_a_reset_just_passed_is_not_read_as_tomorrow(tmp_path, monkeypatch):
    import os
    from datetime import datetime

    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "settings.yaml").write_text(
        'usage_limit_wait_until: "19:00"\n', encoding="utf-8"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    quota = run_dir / "score.quota"
    quota.write_text("You've hit your 5-hour limit · resets 1:10am", encoding="utf-8")
    written = datetime(2026, 11, 7, 1, 5).timestamp()
    os.utime(quota, (written, written))
    monkeypatch.setattr(M.clock, "now", lambda: datetime(2026, 11, 7, 1, 20))
    slept = []
    monkeypatch.setattr(M, "_sleep_until", slept.append)
    night = M.Night(run_dir, {"stages": {}}, False, tmp_path)
    assert night.wait("score", 0) and slept == [datetime(2026, 11, 7, 1, 23)]


def test_score_runs_at_most_judge_parallel_judges_at_once(tmp_path):
    import subprocess
    import sys

    from applyourself.shared import paths

    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "live.log"
    (fake / "claude").write_text(
        "#!/bin/bash\n"
        f'echo start >> "{log}"; sleep 1; echo end >> "{log}"\n'
        'echo \'{"is_error": false, "total_cost_usd": 0.1}\'\n',
        encoding="utf-8",
    )
    lines = "\\n".join(f"judge s{i}.md" for i in range(5))
    (fake / "uv").write_text(  # `config get` -> 2; `score start` -> 5 judge lines
        f'#!/bin/bash\n[[ "$2" == python ]] && {{ shift 2; exec "{sys.executable}" "$@"; }}\n'
        f'[[ "$2" == score && "$3" == start ]] && {{ printf "judge=5\\n{lines}\\n"; exit 0; }}\n'
        '[[ "$2" == score && "$3" == finish ]] && { echo "merged"; exit 0; }\n'
        "echo 2\n",
        encoding="utf-8",
    )
    for f in fake.iterdir():
        f.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "UV": str(fake / "uv"), "CLAUDE": str(fake / "claude")}
    env["RUN_DIR"] = str(tmp_path / "run")
    env["HOME"] = str(tmp_path)
    script = paths.REPO_ROOT / "scripts" / "stages" / "score.sh"
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
    live = peak = 0
    for event in log.read_text(encoding="utf-8").split():
        live += 1 if event == "start" else -1
        peak = max(peak, live)
    assert peak == 2
