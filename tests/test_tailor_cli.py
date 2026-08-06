"""Tests for src/tailor_cli.py — the deterministic front-matter of /tailor.

The conftest autouse `cfg` fixture injects tests/fixtures/verticals.yaml
(default_vertical=example_tertiary; verticals example_primary/example_secondary/
example_tertiary), so vertical resolution runs against that config without
touching the gitignored profile config."""
from __future__ import annotations

import json

import pandas as pd
import pytest
import yaml

from src import tailor_cli, verticals


@pytest.fixture(autouse=True)
def isolate_resume_files(tmp_path, monkeypatch, cfg):
    """prep resolves resume_file against verticals.REPO_ROOT. Repoint it at
    tmp_path and write a stand-in for every configured vertical, so a test
    never depends on the committed example resumes' contents -- same isolation
    pattern as isolate_inbox_path."""
    monkeypatch.setattr(verticals, "REPO_ROOT", tmp_path)
    for v in cfg.verticals.values():
        p = tmp_path / v.resume_file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("**Ada Lovelace**\n\nLondon | ada@example.com\n", encoding="utf-8")
    return tmp_path


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
        (prof / name).write_text(body, encoding="utf-8")


def _write_parquets(tmp_path, clean_rows, scored_rows):
    pd.DataFrame(clean_rows).to_parquet(tmp_path / "clean.parquet")
    pd.DataFrame(scored_rows).to_parquet(tmp_path / "scored.parquet")


def _register(tmp_path, job_id):
    d = tmp_path / "pipeline" / job_id
    d.mkdir(parents=True)
    (d / "state.yaml").write_text(yaml.safe_dump({"job_id": job_id, "state": "saved"}), encoding="utf-8")


def _setup(tmp_path, *, job_id="aaaaaaaa", company="Acme Corp.", title="Widget Functional Consultant",
           vertical="example_primary", diction=True, skip_profile=()):
    _write_parquets(
        tmp_path,
        clean_rows=[{
            "job_id": job_id, "company": company, "title": title,
            "vertical": vertical, "url": "https://x", "posted_date": "2026-07-01",
            "jd_text": "We need a widget functional consultant. Gizmo calibration.",
        }],
        scored_rows=[{
            "job_id": job_id, "fit_score": 80, "vertical": vertical,
            "keywords_to_mirror": ["widget assembly", "gizmo"],
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
    assert ev["VERTICAL"] == "example_primary"
    assert ev["DIRNAME"] == "example_primary/2026-07-24_acme-corp_widget-functional-consultant_aaaaaaaa"
    assert ev["OUT_DIR"].endswith("applications/example_primary/2026-07-24_acme-corp_widget-functional-consultant_aaaaaaaa")
    assert ev["DICTION_PASS"] == "true"
    # side effects
    row_json = tmp_path / "tmp" / "tailor_aaaaaaaa_row.json"
    assert row_json.is_file()
    row = json.loads(row_json.read_text(encoding="utf-8"))
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

def test_prep_unknown_vertical_falls_back_to_default(tmp_path, capsys, cfg):
    _setup(tmp_path, vertical="not_a_vertical")
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    got = _parse_eval(capsys.readouterr().out)["VERTICAL"]
    assert got == cfg.default_vertical


def test_prep_versions_on_retailor(tmp_path, capsys):
    _setup(tmp_path)
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    capsys.readouterr()
    # Second run on a different day still bumps to _v2 (count-based, not date).
    tailor_cli.main(["aaaaaaaa", "--today", "2026-08-01"])
    ev = _parse_eval(capsys.readouterr().out)
    assert ev["DIRNAME"] == "example_primary/2026-08-01_acme-corp_widget-functional-consultant_aaaaaaaa_v2"
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
                     "vertical": "example_primary", "url": "u", "posted_date": "d", "jd_text": "j"}],
        scored_rows=[{"job_id": "zzzzzzzz", "fit_score": 1, "vertical": "example_primary"}],
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
    snap = (out_dir / "jd_snapshot.md").read_text(encoding="utf-8")
    assert snap.startswith("---\njob_id: aaaaaaaa\n")
    assert "company: Acme Corp." in snap
    assert "title: Widget Functional Consultant" in snap
    assert "snapshot_at: 2026-07-24" in snap
    assert snap.rstrip().endswith("Gizmo calibration.")
    assert "chars of JD body" in capsys.readouterr().out


def test_snapshot_missing_row_json_errors(tmp_path):
    (tmp_path / "applications").mkdir()
    with pytest.raises(SystemExit, match="run `tailor-prep"):
        tailor_cli.main(["snapshot", "aaaaaaaa", str(tmp_path / "applications")])


# ---------- versioning: max+1, never count ----------

def _mkdirs(root, vertical, names):
    d = root / "applications" / vertical
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).mkdir()


def test_versioned_dirname_increments_normally(tmp_path):
    base = "2026-08-04_acme_analyst_abc12345"
    args = ("example_primary", "acme", "analyst", "abc12345", "2026-08-04")
    assert tailor_cli._versioned_dirname(*args) == f"example_primary/{base}"
    _mkdirs(tmp_path, "example_primary", [base])
    assert tailor_cli._versioned_dirname(*args) == f"example_primary/{base}_v2"
    _mkdirs(tmp_path, "example_primary", [f"{base}_v2"])
    assert tailor_cli._versioned_dirname(*args) == f"example_primary/{base}_v3"


def test_versioned_dirname_skips_gaps_left_by_deleted_versions(tmp_path):
    """Count-based versioning reused a live name once an intermediate version
    had been pruned: v1+v3 on disk resolved to _v3 again."""
    base = "2026-08-04_acme_analyst_abc12345"
    _mkdirs(tmp_path, "example_primary", [base, f"{base}_v3"])  # v2 was deleted
    got = tailor_cli._versioned_dirname("example_primary", "acme", "analyst", "abc12345", "2026-08-04")
    assert got == f"example_primary/{base}_v4"
    assert not (tmp_path / "applications" / got).exists()


def test_versioned_dirname_ignores_other_job_ids(tmp_path):
    base = "2026-08-04_acme_analyst_abc12345"
    _mkdirs(tmp_path, "example_primary", ["2026-08-04_other_role_99999999",
                              "2026-08-04_other_role_99999999_v2"])
    got = tailor_cli._versioned_dirname("example_primary", "acme", "analyst", "abc12345", "2026-08-04")
    assert got == f"example_primary/{base}"


def test_versioned_dirname_uses_todays_date_not_the_originals(tmp_path):
    _mkdirs(tmp_path, "example_primary", ["2026-06-01_acme_analyst_abc12345"])
    got = tailor_cli._versioned_dirname("example_primary", "acme", "analyst", "abc12345", "2026-08-04")
    assert got == "example_primary/2026-08-04_acme_analyst_abc12345_v2"


# ---------------- identity: name + file slug from resume_file ----------------

@pytest.mark.parametrize("line", [
    "**Ada Lovelace**",
    "# **Ada Lovelace**",
    "  **Ada Lovelace**  ",
])
def test_resume_display_name_accepts_the_docx_render_name_shapes(tmp_path, line):
    """Must match what src/docx_render.py parses as the `name` block:
    `**Name**` or `# **Name**`, first such line only."""
    p = tmp_path / "r.md"
    p.write_text(f"{line}\n\nLondon | ada@example.com\n\n**SUMMARY**\n", encoding="utf-8")
    assert tailor_cli.resume_display_name(p) == "Ada Lovelace"


def test_resume_display_name_takes_the_first_bold_line_only(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("**Ada Lovelace**\n\n**SUMMARY**\n\n**WORK EXPERIENCE**\n", encoding="utf-8")
    assert tailor_cli.resume_display_name(p) == "Ada Lovelace"


def test_resume_display_name_missing_file_errors(tmp_path):
    with pytest.raises(SystemExit, match="resume_file must exist"):
        tailor_cli.resume_display_name(tmp_path / "nope.md")


@pytest.mark.parametrize("body", ["", "\n\n", "Ada Lovelace\n", "**  **\n"])
def test_resume_display_name_without_a_name_line_errors(tmp_path, body):
    p = tmp_path / "r.md"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match="no name line"):
        tailor_cli.resume_display_name(p)


@pytest.mark.parametrize("name,expected", [
    ("Ada Lovelace", "Ada_Lovelace"),
    ("Ada B. Lovelace", "Ada_B_Lovelace"),
    ("O'Brien", "O_Brien"),
    ("  Ada   Lovelace  ", "Ada_Lovelace"),
    ("José Álvarez", "José_Álvarez"),      # accents survive, not stripped
    ("Ada Lovelace 2nd", "Ada_Lovelace_2nd"),
])
def test_file_slug(name, expected):
    assert tailor_cli.file_slug(name) == expected


def test_prep_emits_applicant_name_and_file_slug(tmp_path, capsys):
    _setup(tmp_path)
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    ev = _parse_eval(capsys.readouterr().out)
    assert ev["APPLICANT_NAME"] == "Ada Lovelace"
    assert ev["FILE_SLUG"] == "Ada_Lovelace"


def test_identity_subcommand_prints_only_the_two_vars(tmp_path, capsys):
    rc = tailor_cli.main(["identity", "example_primary"])
    assert rc == 0
    out = capsys.readouterr().out
    assert _parse_eval(out) == {"APPLICANT_NAME": "Ada Lovelace", "FILE_SLUG": "Ada_Lovelace"}
    assert "OUT_DIR" not in out  # identity never creates or resolves a dir


def test_identity_unknown_vertical_errors_and_names_the_configured_ones(tmp_path):
    with pytest.raises(SystemExit, match="unknown vertical 'nope'"):
        tailor_cli.main(["identity", "nope"])


def test_identity_missing_resume_file_errors(tmp_path, cfg):
    (tmp_path / cfg.verticals["example_primary"].resume_file).unlink()
    with pytest.raises(SystemExit, match="resume_file must exist"):
        tailor_cli.main(["identity", "example_primary"])


def test_prep_eval_output_survives_an_apostrophe_in_the_name(tmp_path, capsys, cfg):
    """A name is user-authored text, so `eval "$(tailor-prep ...)"` must not
    break on O'Brien -- the reason every value goes through shlex.quote."""
    import subprocess
    for v in cfg.verticals.values():
        (tmp_path / v.resume_file).write_text("**Grace O'Brien**\n", encoding="utf-8")
    _setup(tmp_path)
    tailor_cli.main(["aaaaaaaa", "--today", "2026-07-24"])
    out = capsys.readouterr().out
    got = subprocess.run(
        ["bash", "-c", f'eval "$(cat <<\'EOF\'\n{out}\nEOF\n)"; printf "%s|%s" "$APPLICANT_NAME" "$FILE_SLUG"'],
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout
    assert got == "Grace O'Brien|Grace_O_Brien"


def test_resume_files_are_read_from_tmp_not_the_real_profile(tmp_path, cfg):
    """Isolation guard: the injected config's resume_file paths are the real
    gitignored ones, so a regression here would read the user's own resume."""
    assert tailor_cli._identity_for("example_primary") == ("Ada Lovelace", "Ada_Lovelace")
    assert (tmp_path / cfg.verticals["example_primary"].resume_file).is_file()
