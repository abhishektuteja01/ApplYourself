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
