"""scripts/import_companies_csv.py: slug reshaping and the overlay merge.

The script's whole job is two claims: a Workday board URL becomes our tri-part
slug, and a bulk dump can neither clobber a curated overlay row nor resurrect a
tombstoned one. Those are what is tested here; the summary table is print-only.
"""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "import_companies_csv.py"


@pytest.fixture(scope="module")
def imp():
    spec = importlib.util.spec_from_file_location("import_companies_csv", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ats", "name", "slug", "url"])
        w.writerows(rows)
    return path


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        return next(reader), list(reader)


def _run(imp, tmp_path, rows, *, extra_argv=()):
    """Write a source dump plus an empty universe dir, then run main()."""
    universe = tmp_path / "universe"
    universe.mkdir(exist_ok=True)
    src = _source(tmp_path / "companies.csv", rows)
    argv = ["--source", str(src), "--universe-dir", str(universe), *extra_argv]
    rc = imp.main(argv)
    return rc, universe


# --- Workday URL -> tenant|pod|site_id -------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://23andme.wd5.myworkdayjobs.com/23", "23andme|wd5|23"),
    ("https://3i.wd103.myworkdayjobs.com/Direct_Hire", "3i|wd103|Direct_Hire"),
    ("https://x.wd1.myworkdayjobs.com/en-US/SiteName", "x|wd1|SiteName"),
    ("http://x.WD2.MyWorkdayJobs.com/careers", "x|WD2|careers"),
])
def test_workday_slug(imp, url, expected):
    assert imp.workday_slug(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "https://boards.greenhouse.io/acme",
    "https://x.wdX.myworkdayjobs.com/site",
    "https://x.wd1.myworkdayjobs.com/",
    "https://x.wd1.myworkdayjobs.com/en-US/a/b",
])
def test_workday_slug_rejects(imp, url):
    with pytest.raises(ValueError):
        imp.workday_slug(url)


def test_workday_row_uses_url_not_slug_column(imp, tmp_path):
    rc, universe = _run(imp, tmp_path, [
        ["workday", "23Andme", "23andme/23", "https://23andme.wd5.myworkdayjobs.com/23"],
    ])
    assert rc == 0
    _, rows = _read(universe / "workday.local.csv")
    assert rows == [["23Andme", "23andme|wd5|23", "1"]]


def test_unparseable_workday_url_is_counted_not_fatal(imp, tmp_path, capsys):
    rc, universe = _run(imp, tmp_path, [
        ["workday", "Broken", "broken/x", "https://broken.example.com/jobs"],
        ["workday", "Good", "good/x", "https://good.wd1.myworkdayjobs.com/ext"],
    ])
    assert rc == 0
    _, rows = _read(universe / "workday.local.csv")
    assert rows == [["Good", "good|wd1|ext", "1"]]
    out = capsys.readouterr().out
    # in_source added already_known tombstoned unparseable
    assert [c for c in out.splitlines() if c.startswith("workday")][0].split()[1:] == \
        ["2", "1", "0", "0", "1"]


def test_workday_slug_rejected_by_parse_slug_is_skipped(imp, tmp_path, monkeypatch):
    def reject(slug):
        raise ValueError("no")

    monkeypatch.setattr(imp, "parse_slug", reject)
    rc, universe = _run(imp, tmp_path, [
        ["workday", "Good", "good/x", "https://good.wd1.myworkdayjobs.com/ext"],
    ])
    assert rc == 0
    assert not (universe / "workday.local.csv").exists()


# --- tombstones ------------------------------------------------------------

def test_tracked_tombstone_is_not_re_added(imp, tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "lever.csv").write_text("name,slug,active\nDead Co,dead,0\n", encoding="utf-8")
    rc, _ = _run(imp, tmp_path, [
        ["lever", "Dead Co", "dead", "https://jobs.lever.co/dead"],
        ["lever", "Live Co", "live", "https://jobs.lever.co/live"],
    ])
    assert rc == 0
    _, rows = _read(universe / "lever.local.csv")
    assert rows == [["Live Co", "live", "1"]]


def test_local_tombstone_is_not_re_added(imp, tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "lever.local.csv").write_text(
        "name,slug,active\nDead Co,dead,0\n", encoding="utf-8")
    rc, _ = _run(imp, tmp_path, [
        ["lever", "Dead Co Renamed", "dead", "https://jobs.lever.co/dead"],
        ["lever", "Live Co", "live", "https://jobs.lever.co/live"],
    ])
    assert rc == 0
    _, rows = _read(universe / "lever.local.csv")
    # The tombstone stays, with its own name, and keeps active=0.
    assert rows == [["Dead Co", "dead", "0"], ["Live Co", "live", "1"]]


def test_tracked_live_slug_counts_as_already_known(imp, tmp_path, capsys):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "ashby" ".csv").write_text("name,slug\nKnown Co,known\n", encoding="utf-8")
    rc, _ = _run(imp, tmp_path, [
        ["ashby", "Known Co", "known", "https://jobs.ashbyhq.com/known"],
    ])
    assert rc == 0
    # Nothing new, so no overlay is created at all.
    assert not (universe / "ashby.local.csv").exists()
    line = [c for c in capsys.readouterr().out.splitlines() if c.startswith("ashby")][0]
    assert line.split()[1:] == ["1", "0", "1", "0", "0"]


# --- merge, shape, dry run -------------------------------------------------

def test_existing_local_row_survives_the_merge(imp, tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "greenhouse.local.csv").write_text(
        "name,slug,active\nCurated Name,shared,1\n", encoding="utf-8")
    rc, _ = _run(imp, tmp_path, [
        ["greenhouse", "Bulk Name", "shared", "https://boards.greenhouse.io/shared"],
        ["greenhouse", "New Co", "new", "https://boards.greenhouse.io/new"],
    ])
    assert rc == 0
    _, rows = _read(universe / "greenhouse.local.csv")
    assert rows == [["New Co", "new", "1"], ["Curated Name", "shared", "1"]]


def test_output_is_header_sorted_and_deduped(imp, tmp_path):
    rc, universe = _run(imp, tmp_path, [
        ["lever", "Zeta", "zeta", "https://jobs.lever.co/zeta"],
        ["lever", "Alpha", "alpha", "https://jobs.lever.co/alpha"],
        ["lever", "Alpha Again", "alpha", "https://jobs.lever.co/alpha"],
        ["lever", "Mid", "mid", "https://jobs.lever.co/mid"],
    ])
    assert rc == 0
    header, rows = _read(universe / "lever.local.csv")
    assert header == ["name", "slug", "active"]
    assert [r[1] for r in rows] == ["alpha", "mid", "zeta"]
    # First spelling of a duplicated slug wins.
    assert rows[0][0] == "Alpha"


def test_dry_run_writes_nothing(imp, tmp_path, capsys):
    rc, universe = _run(imp, tmp_path, [
        ["lever", "Alpha", "alpha", "https://jobs.lever.co/alpha"],
    ], extra_argv=["--dry-run"])
    assert rc == 0
    assert list(universe.iterdir()) == []
    assert "--dry-run" in capsys.readouterr().out


def test_ats_flag_limits_the_lanes(imp, tmp_path):
    rc, universe = _run(imp, tmp_path, [
        ["lever", "Alpha", "alpha", "https://jobs.lever.co/alpha"],
        ["ashby", "Beta", "beta", "https://jobs.ashbyhq.com/beta"],
    ], extra_argv=["--ats", "lever"])
    assert rc == 0
    assert [p.name for p in universe.iterdir()] == ["lever.local.csv"]


def test_unknown_ats_row_is_skipped(imp, tmp_path, capsys):
    rc, universe = _run(imp, tmp_path, [
        ["smartrecruiters", "Other", "other", "https://example.com/other"],
        ["lever", "Alpha", "alpha", "https://jobs.lever.co/alpha"],
    ])
    assert rc == 0
    assert [p.name for p in universe.iterdir()] == ["lever.local.csv"]
    assert "smartrecruiters" in capsys.readouterr().err


def test_missing_source_and_bad_columns_exit_1(imp, tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    assert imp.main(["--source", str(tmp_path / "nope.csv"),
                     "--universe-dir", str(universe)]) == 1
    bad = tmp_path / "bad.csv"
    bad.write_text("ats,name\nlever,Alpha\n", encoding="utf-8")
    assert imp.main(["--source", str(bad), "--universe-dir", str(universe)]) == 1


def test_written_overlay_reads_back_through_universe(imp, tmp_path, monkeypatch):
    """The point of the format: what this writes is what `universe` loads."""
    from src.discovery import universe as uni

    rc, universe_dir = _run(imp, tmp_path, [
        ["workday", "Good", "good/x", "https://good.wd1.myworkdayjobs.com/ext"],
    ])
    assert rc == 0
    monkeypatch.setattr(uni, "CSV_DIR", universe_dir)
    monkeypatch.setattr(uni, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(uni, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    assert uni.universe_slugs("workday") == {"good|wd1|ext"}
