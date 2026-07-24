"""Tests for src/tailor_cli.py — the deterministic front-matter of /tailor.

The conftest autouse `cfg` fixture injects tests/fixtures/verticals.yaml
(default_vertical=sap; verticals sap/ai_eng/risk_ai), so vertical resolution
runs against that config without touching the gitignored profile config."""
from __future__ import annotations

import json

import pandas as pd
import pytest
import yaml

from src import tailor_cli


def _parse_eval(out: str) -> dict:
    """Turn the KEY='value' stdout block into a dict."""
    result = {}
    for line in out.strip().splitlines():
        key, _, val = line.partition("=")
        result[key] = val.strip().strip("'")
    return result


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(tailor_cli, "CLEAN", tmp_path / "clean.parquet")
    monkeypatch.setattr(tailor_cli, "SCORED", tmp_path / "scored.parquet")
    monkeypatch.setattr(tailor_cli, "PIPELINE", tmp_path / "pipeline")
    monkeypatch.setattr(tailor_cli, "PROFILE", tmp_path / "profile")
    monkeypatch.setattr(tailor_cli, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(tailor_cli, "TMPDIR", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _write_profile(tmp_path, *, diction=True, skip=()):
    prof = tmp_path / "profile"
    prof.mkdir(exist_ok=True)
    files = {
        "bullets.md": "canonical bullets",
        "de_ai_rules.yaml": (
            "bullets_diction_pass_completed: true\n" if diction
            else "bullets_diction_pass_completed: false\n"
        ),
        "skills_master.md": "skills",
        "resume_template.docx": "docx",
    }
    for name, body in files.items():
        if name in skip:
            continue
        (prof / name).write_text(body)


def _write_parquets(tmp_path, clean_rows, scored_rows):
    pd.DataFrame(clean_rows).to_parquet(tmp_path / "clean.parquet")
    pd.DataFrame(scored_rows).to_parquet(tmp_path / "scored.parquet")


def _register(tmp_path, job_id):
    d = tmp_path / "pipeline" / job_id
    d.mkdir(parents=True)
    (d / "state.yaml").write_text(yaml.safe_dump({"job_id": job_id, "state": "saved"}))


def _setup(tmp_path, *, job_id="aaaaaaaa", company="Acme Corp.", title="SAP SD Consultant",
           vertical="sap", diction=True, skip_profile=()):
    _write_parquets(
        tmp_path,
        clean_rows=[{
            "job_id": job_id, "company": company, "title": title,
            "vertical": vertical, "url": "https://x", "posted_date": "2026-07-01",
            "jd_text": "We need an SAP SD consultant. Order-to-cash.",
        }],
        scored_rows=[{
            "job_id": job_id, "fit_score": 80, "vertical": vertical,
            "keywords_to_mirror": ["SD", "O2C"],
        }],
    )
    _write_profile(tmp_path, diction=diction, skip=skip_profile)
    _register(tmp_path, job_id)


# ---------------- prep: happy path ----------------

def test_prep_creates_dir_row_json_and_prints_eval_vars(tmp_path, capsys):
    _setup(tmp_path)
    rc = tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    assert rc == 0
    ev = _parse_eval(capsys.readouterr().out)
    assert ev["VERTICAL"] == "sap"
    assert ev["DIRNAME"] == "sap/2026-07-24_acme-corp_sap-sd-consultant_aaaaaaaa"
    assert ev["OUT_DIR"].endswith("applications/sap/2026-07-24_acme-corp_sap-sd-consultant_aaaaaaaa")
    assert ev["DICTION_PASS"] == "true"
    # side effects
    row_json = tmp_path / "tmp" / "tailor_aaaaaaaa_row.json"
    assert row_json.is_file()
    row = json.loads(row_json.read_text())
    assert row["fit_score"] == 80 and row["company"] == "Acme Corp."
    assert (tmp_path / "applications" / ev["DIRNAME"]).is_dir()


def test_prep_diction_false_reflected(tmp_path, capsys):
    _setup(tmp_path, diction=False)
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    assert _parse_eval(capsys.readouterr().out)["DICTION_PASS"] == "false"


def test_prep_stderr_carries_row_json(tmp_path, capsys):
    _setup(tmp_path)
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    err = capsys.readouterr().err
    assert "tailoring to:" in err
    assert '"fit_score": 80' in err


# ---------------- prep: vertical resolution + versioning ----------------

def test_prep_unknown_vertical_falls_back_to_default(tmp_path, capsys):
    _setup(tmp_path, vertical="not_a_vertical")
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    assert _parse_eval(capsys.readouterr().out)["VERTICAL"] == "sap"


def test_prep_versions_on_retailor(tmp_path, capsys):
    _setup(tmp_path)
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    capsys.readouterr()
    # Second run on a different day still bumps to _v2 (count-based, not date).
    tailor_cli.main(["aaaaaaaa", "--today", "2026-08-01"])
    ev = _parse_eval(capsys.readouterr().out)
    assert ev["DIRNAME"] == "sap/2026-08-01_acme-corp_sap-sd-consultant_aaaaaaaa_v2"
    assert (tmp_path / "applications" / ev["DIRNAME"]).is_dir()


# ---------------- prep: fail-loud prereqs ----------------

def test_prep_missing_clean_parquet_errors(tmp_path):
    _setup(tmp_path)
    (tmp_path / "clean.parquet").unlink()
    with pytest.raises(SystemExit, match="clean.parquet missing"):
        tailor_cli.main(["aaaaaaaa"])


def test_prep_missing_scored_parquet_errors(tmp_path):
    _setup(tmp_path)
    (tmp_path / "scored.parquet").unlink()
    with pytest.raises(SystemExit, match="scored.parquet missing"):
        tailor_cli.main(["aaaaaaaa"])


def test_prep_job_id_absent_from_scored_errors(tmp_path):
    _write_parquets(
        tmp_path,
        clean_rows=[{"job_id": "aaaaaaaa", "company": "A", "title": "T",
                     "vertical": "sap", "url": "u", "posted_date": "d", "jd_text": "j"}],
        scored_rows=[{"job_id": "zzzzzzzz", "fit_score": 1, "vertical": "sap"}],
    )
    _write_profile(tmp_path)
    _register(tmp_path, "aaaaaaaa")
    with pytest.raises(SystemExit, match="not in scored.parquet"):
        tailor_cli.main(["aaaaaaaa"])


def test_prep_missing_state_yaml_errors(tmp_path):
    _setup(tmp_path)
    (tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml").unlink()
    with pytest.raises(SystemExit, match="state.yaml missing"):
        tailor_cli.main(["aaaaaaaa"])


def test_prep_missing_profile_file_errors(tmp_path):
    _setup(tmp_path, skip_profile=("skills_master.md",))
    with pytest.raises(SystemExit, match="profile/skills_master.md missing"):
        tailor_cli.main(["aaaaaaaa"])


def test_prep_does_not_require_preferences_md(tmp_path, capsys):
    """Regression: preferences.md is not a /tailor prereq (dropped)."""
    _setup(tmp_path)
    assert not (tmp_path / "profile" / "preferences.md").exists()
    assert tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"]) == 0


# ---------------- snapshot ----------------

def test_snapshot_writes_from_row_json_without_reading_parquet(tmp_path, capsys):
    _setup(tmp_path)
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    ev = _parse_eval(capsys.readouterr().out)
    out_dir = tmp_path / "applications" / ev["DIRNAME"]
    # Deleting the parquet proves snapshot reads only the row.json.
    (tmp_path / "clean.parquet").unlink()
    rc = tailor_cli.main(["snapshot", "aaaaaaaa", str(out_dir), "--today", "2026-07-24"])
    assert rc == 0
    snap = (out_dir / "jd_snapshot.md").read_text()
    assert snap.startswith("---\njob_id: aaaaaaaa\n")
    assert "company: Acme Corp." in snap
    assert "title: SAP SD Consultant" in snap
    assert "snapshot_at: 2026-07-24" in snap
    assert snap.rstrip().endswith("Order-to-cash.")
    assert "chars of JD body" in capsys.readouterr().out


def test_snapshot_missing_row_json_errors(tmp_path):
    (tmp_path / "applications").mkdir()
    with pytest.raises(SystemExit, match="run `tailor-prep"):
        tailor_cli.main(["snapshot", "aaaaaaaa", str(tmp_path / "applications")])
