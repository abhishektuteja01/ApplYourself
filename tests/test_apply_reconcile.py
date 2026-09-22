"""scripts/apply_reconcile.py: the only writer of the `active` column.

Every test runs against a `--universe-dir` under `tmp_path`, so the real
`data/universe/` is never opened. The bulk of the file is the refusal path:
retirement is reversible only by hand, so what matters is not that a valid
proposal applies but that an invalid one cannot.
"""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "apply_reconcile.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("apply_reconcile", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def R(mod):
    return mod.R


# --- fixtures ---------------------------------------------------------------

def write_universe(directory: Path, name: str, rows) -> Path:
    """rows: (name, slug) or (name, slug, active)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "slug", "active"])
        for row in rows:
            w.writerow(list(row) + [""] * (3 - len(row)))
    return path


def read_universe(path: Path) -> dict:
    with open(path, encoding="utf-8", newline="") as f:
        return {r["slug"]: r for r in csv.DictReader(f)}


def slug_order(path: Path) -> list:
    with open(path, encoding="utf-8", newline="") as f:
        return [r["slug"] for r in csv.DictReader(f)]


def proposal(mod, tmp_path: Path, rows) -> Path:
    """One proposal CSV. Each row is a dict of overrides on a valid `moved`."""
    path = tmp_path / "slug_reconcile_test.csv"
    base = {
        "ats": "greenhouse", "slug": "old", "name": "Acme", "old_status": "dead",
        "verdict": "moved", "new_ats": "ashby", "new_slug": "new", "new_jobs": "9",
        "acquirer": "", "confidence": "high", "domain": "", "evidence": "t2",
    }
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=mod.R.COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow({**base, **row})
    return path


@pytest.fixture
def universe_dir(tmp_path):
    d = tmp_path / "universe"
    write_universe(d, "greenhouse.csv", [("Acme", "old"), ("Zeta", "zeta")])
    return d


def run(mod, prop: Path, universe_dir: Path, *args, log=None):
    argv = ["--proposal", str(prop), "--universe-dir", str(universe_dir)]
    argv += ["--log", str(log or (universe_dir.parent / "log.csv"))]
    return mod.main(argv + list(args))


# --- the verdict table ------------------------------------------------------

def test_moved_retires_the_old_row_and_adds_the_new_slug(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [{}])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 0

    tracked = read_universe(universe_dir / "greenhouse.csv")
    assert tracked["old"]["active"] == "0"
    assert tracked["old"]["name"] == "Acme"
    assert tracked["zeta"]["active"] == ""          # untouched
    added = read_universe(universe_dir / "ashby.local.csv")
    assert added["new"]["active"] == "1"
    assert added["new"]["name"] == "Acme"


def test_gone_retires_and_adds_nothing(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [
        {"verdict": "gone", "new_ats": "", "new_slug": "", "new_jobs": ""}])
    assert run(mod, prop, universe_dir, "--verdict", "gone", "--no-backup") == 0
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == "0"
    assert not list(universe_dir.glob("*.local.csv"))


def test_dual_adds_and_never_touches_the_old_row(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [{"verdict": "dual"}])
    assert run(mod, prop, universe_dir, "--verdict", "dual", "--no-backup") == 0
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == ""
    assert read_universe(universe_dir / "ashby.local.csv")["new"]["active"] == "1"


@pytest.mark.parametrize("verdict", ["ok", "alive_no_board", "review", "error"])
def test_inert_verdicts_do_nothing_even_when_selected(mod, tmp_path, universe_dir, verdict):
    prop = proposal(mod, tmp_path, [{"verdict": verdict}])
    before = (universe_dir / "greenhouse.csv").read_text(encoding="utf-8")
    assert run(mod, prop, universe_dir, "--verdict", verdict, "--no-backup") == 0
    assert (universe_dir / "greenhouse.csv").read_text(encoding="utf-8") == before
    assert not list(universe_dir.glob("*.local.csv"))


def test_moved_offsite_retires_but_adds_nothing(mod, tmp_path, universe_dir, R, monkeypatch):
    """The destination platform is not a lane we scrape, so there is no board
    to add. Retirement still needs the resolver module to class the verdict as
    retireable."""
    monkeypatch.setattr(R, "VERDICTS", tuple(R.VERDICTS) + (mod.MOVED_OFFSITE,))
    monkeypatch.setattr(R, "RETIREABLE", frozenset(R.RETIREABLE | {mod.MOVED_OFFSITE}))
    prop = proposal(mod, tmp_path, [
        {"verdict": mod.MOVED_OFFSITE, "new_ats": "workable", "new_slug": "acme"}])
    assert run(mod, prop, universe_dir, "--verdict", mod.MOVED_OFFSITE, "--no-backup") == 0
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == "0"
    assert not list(universe_dir.glob("*.local.csv"))
    with open(tmp_path / "log.csv", encoding="utf-8", newline="") as f:
        log = list(csv.DictReader(f))
    assert any(r["action"] == "skipped" and "workable/acme" in r["detail"] for r in log)


def test_moved_offsite_is_refused_while_the_resolver_denies_it(mod, tmp_path, universe_dir, R):
    """Authorization comes from `slug_resolvers`, so a verdict that module does
    not class as retireable cannot retire, whatever the CSV says."""
    # `mod.retires`, not `R.is_retireable`: authorization flows through the
    # resolver's `retires_old_row` once it exists, because `moved_offsite`
    # retires the old row without adding a board and so is deliberately
    # outside `RETIREABLE`.
    if mod.retires(mod.MOVED_OFFSITE):
        pytest.skip("the resolver module already authorizes moved_offsite")
    prop = proposal(mod, tmp_path, [
        {"verdict": mod.MOVED_OFFSITE, "new_ats": "workable", "new_slug": "acme"}])
    assert run(mod, prop, universe_dir, "--verdict", mod.MOVED_OFFSITE, "--no-backup") == 1
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == ""


# --- selection and dry-run --------------------------------------------------

def test_a_bare_invocation_writes_nothing(mod, tmp_path, universe_dir, capsys):
    prop = proposal(mod, tmp_path, [{}])
    before = (universe_dir / "greenhouse.csv").read_text(encoding="utf-8")
    assert run(mod, prop, universe_dir) == 0
    assert (universe_dir / "greenhouse.csv").read_text(encoding="utf-8") == before
    assert not list(universe_dir.glob("*.local.csv"))
    assert not (tmp_path / "log.csv").exists()
    out = capsys.readouterr().out
    assert "writing nothing" in out
    assert "retire" in out       # it still says what it would do


def test_dry_run_writes_nothing(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [{}])
    before = (universe_dir / "greenhouse.csv").read_text(encoding="utf-8")
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--dry-run") == 0
    assert (universe_dir / "greenhouse.csv").read_text(encoding="utf-8") == before
    assert not list(universe_dir.glob("*.local.csv"))
    assert not (tmp_path / "log.csv").exists()
    assert not list(universe_dir.glob("*.bak-*"))


def test_naming_one_verdict_leaves_the_others_alone(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [
        {},
        {"slug": "zeta", "name": "Zeta", "verdict": "gone",
         "new_ats": "", "new_slug": ""},
    ])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 0
    tracked = read_universe(universe_dir / "greenhouse.csv")
    assert tracked["old"]["active"] == "0"
    assert tracked["zeta"]["active"] == ""


# --- refusals ---------------------------------------------------------------

@pytest.mark.parametrize("status", ["empty", "stale", "active"])
def test_a_hand_edited_status_is_refused(mod, tmp_path, universe_dir, status):
    """The hand-edited-CSV case: `moved` on a board that answered 200."""
    prop = proposal(mod, tmp_path, [{"old_status": status}])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 1
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == ""
    # A refused row does not half-apply as a bare addition either.
    assert not list(universe_dir.glob("*.local.csv"))


def test_low_confidence_is_refused(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [{"confidence": "low"}])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 1
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == ""
    assert not list(universe_dir.glob("*.local.csv"))


def test_a_moved_row_with_no_destination_is_refused(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [{"new_slug": "", "new_ats": ""}])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 1
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == ""


def test_an_unknown_verdict_is_refused_not_acted_on(mod, tmp_path, universe_dir, capsys):
    prop = proposal(mod, tmp_path, [{"verdict": "retire_now"}])
    assert run(mod, prop, universe_dir) == 1
    assert read_universe(universe_dir / "greenhouse.csv")["old"]["active"] == ""
    out = capsys.readouterr().out
    assert "unknown verdict" in out
    assert "retire_now" in out


def test_an_unsupported_destination_lane_is_refused(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [{"verdict": "dual", "new_ats": "workable"}])
    assert run(mod, prop, universe_dir, "--verdict", "dual", "--no-backup") == 1
    assert not list(universe_dir.glob("*.local.csv"))


def test_refusals_still_let_the_valid_rows_through(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [
        {"slug": "zeta", "name": "Zeta", "confidence": "low"},
        {},
    ])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 1
    tracked = read_universe(universe_dir / "greenhouse.csv")
    assert tracked["old"]["active"] == "0"
    assert tracked["zeta"]["active"] == ""


# --- skips ------------------------------------------------------------------

def test_an_already_tombstoned_slug_is_not_rewritten(mod, tmp_path, capsys):
    d = tmp_path / "universe"
    write_universe(d, "greenhouse.csv", [("Acme", "old", "0")])
    before = (d / "greenhouse.csv").read_text(encoding="utf-8")
    prop = proposal(mod, tmp_path, [
        {"verdict": "gone", "new_ats": "", "new_slug": ""}])
    assert run(mod, prop, d, "--verdict", "gone", "--no-backup") == 0
    assert (d / "greenhouse.csv").read_text(encoding="utf-8") == before
    assert not list(d.glob("*.bak-*"))
    assert "already_retired" in capsys.readouterr().out


def test_a_slug_in_no_universe_file_creates_no_tombstone(mod, tmp_path, capsys):
    d = tmp_path / "universe"
    write_universe(d, "greenhouse.csv", [("Zeta", "zeta")])
    prop = proposal(mod, tmp_path, [
        {"verdict": "gone", "new_ats": "", "new_slug": ""}])
    assert run(mod, prop, d, "--verdict", "gone", "--no-backup") == 0
    assert "old" not in read_universe(d / "greenhouse.csv")
    assert not list(d.glob("*.local.csv"))
    assert "not_in_universe" in capsys.readouterr().out


def test_an_addition_that_is_already_listed_is_not_duplicated(mod, tmp_path, universe_dir):
    write_universe(universe_dir, "ashby.local.csv", [("Acme New", "new")])
    prop = proposal(mod, tmp_path, [{"verdict": "dual"}])
    assert run(mod, prop, universe_dir, "--verdict", "dual", "--no-backup") == 0
    rows = slug_order(universe_dir / "ashby.local.csv")
    assert rows == ["new"]
    assert read_universe(universe_dir / "ashby.local.csv")["new"]["name"] == "Acme New"


def test_an_addition_onto_a_tombstone_does_not_resurrect_it(mod, tmp_path, universe_dir):
    write_universe(universe_dir, "ashby.csv", [("Acme New", "new", "0")])
    prop = proposal(mod, tmp_path, [{"verdict": "dual"}])
    assert run(mod, prop, universe_dir, "--verdict", "dual", "--no-backup") == 0
    assert read_universe(universe_dir / "ashby.csv")["new"]["active"] == "0"
    assert not (universe_dir / "ashby.local.csv").exists()


# --- file handling ----------------------------------------------------------

def test_retirement_deletes_nothing_and_keeps_the_sort_order(mod, tmp_path):
    d = tmp_path / "universe"
    write_universe(d, "greenhouse.csv",
                   [("Alpha", "alpha"), ("Acme", "old"), ("Zeta", "zeta")])
    prop = proposal(mod, tmp_path, [
        {"verdict": "gone", "new_ats": "", "new_slug": ""}])
    assert run(mod, prop, d, "--verdict", "gone", "--no-backup") == 0
    assert slug_order(d / "greenhouse.csv") == ["alpha", "old", "zeta"]
    rows = read_universe(d / "greenhouse.csv")
    assert rows["old"]["name"] == "Acme"
    assert rows["alpha"]["active"] == "" and rows["zeta"]["active"] == ""


def test_additions_land_in_the_overlay_never_the_tracked_csv(mod, tmp_path, universe_dir):
    write_universe(universe_dir, "ashby.csv", [("Bravo", "bravo"), ("Yankee", "yankee")])
    prop = proposal(mod, tmp_path, [{"verdict": "dual"}])
    assert run(mod, prop, universe_dir, "--verdict", "dual", "--no-backup") == 0
    assert slug_order(universe_dir / "ashby.csv") == ["bravo", "yankee"]
    assert slug_order(universe_dir / "ashby.local.csv") == ["new"]


def test_an_addition_keeps_the_overlay_sorted(mod, tmp_path, universe_dir):
    write_universe(universe_dir, "ashby.local.csv", [("Alpha", "alpha"), ("Zeta", "zeta")])
    prop = proposal(mod, tmp_path, [{"verdict": "dual"}])
    assert run(mod, prop, universe_dir, "--verdict", "dual", "--no-backup") == 0
    assert slug_order(universe_dir / "ashby.local.csv") == ["alpha", "new", "zeta"]


def test_the_slug_is_retired_in_whichever_file_holds_it(mod, tmp_path):
    d = tmp_path / "universe"
    write_universe(d, "greenhouse.csv", [("Zeta", "zeta")])
    write_universe(d, "greenhouse.local.csv", [("Acme", "old")])
    prop = proposal(mod, tmp_path, [
        {"verdict": "gone", "new_ats": "", "new_slug": ""}])
    assert run(mod, prop, d, "--verdict", "gone", "--no-backup") == 0
    assert read_universe(d / "greenhouse.local.csv")["old"]["active"] == "0"
    assert "old" not in read_universe(d / "greenhouse.csv")


def test_backup_is_written_by_default_and_suppressed_by_no_backup(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [
        {"verdict": "gone", "new_ats": "", "new_slug": ""}])
    original = (universe_dir / "greenhouse.csv").read_text(encoding="utf-8")
    assert run(mod, prop, universe_dir, "--verdict", "gone") == 0
    backups = list(universe_dir.glob("greenhouse.csv.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original

    d2 = tmp_path / "universe2"
    write_universe(d2, "greenhouse.csv", [("Acme", "old")])
    assert run(mod, prop, d2, "--verdict", "gone", "--no-backup") == 0
    assert not list(d2.glob("*.bak-*"))


def test_no_temp_file_is_left_behind(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [{}])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 0
    assert not list(universe_dir.glob("*.tmp"))


# --- the log ----------------------------------------------------------------

def test_the_log_records_every_change(mod, tmp_path, universe_dir):
    prop = proposal(mod, tmp_path, [
        {},
        {"slug": "zeta", "name": "Zeta", "confidence": "low"},
    ])
    log = tmp_path / "changes.csv"
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup", log=log) == 1
    with open(log, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == mod.LOG_COLUMNS
        rows = list(reader)
    by_action = {r["action"]: r for r in rows}
    assert by_action["retire"]["slug"] == "old"
    assert by_action["retire"]["file"] == "greenhouse.csv"
    assert by_action["add"]["slug"] == "new"
    assert by_action["add"]["file"] == "ashby.local.csv"
    assert "confidence" in by_action["refused"]["detail"]


def test_a_missing_proposal_is_an_error(mod, tmp_path, universe_dir):
    assert mod.main(["--proposal", str(tmp_path / "nope.csv"),
                     "--universe-dir", str(universe_dir)]) == 1


def test_a_proposal_missing_columns_is_an_error(mod, tmp_path, universe_dir):
    bad = tmp_path / "bad.csv"
    bad.write_text("ats,slug\ngreenhouse,old\n", encoding="utf-8")
    assert mod.main(["--proposal", str(bad), "--universe-dir", str(universe_dir)]) == 1


# --- round trip -------------------------------------------------------------

def test_what_this_writes_is_what_universe_loads(mod, tmp_path, universe_dir, monkeypatch):
    from src.discovery import universe as uni

    prop = proposal(mod, tmp_path, [{}])
    assert run(mod, prop, universe_dir, "--verdict", "moved", "--no-backup") == 0

    monkeypatch.setattr(uni, "CSV_DIR", universe_dir)
    monkeypatch.setattr(uni, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(uni, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    assert uni.universe_slugs("greenhouse") == {"zeta"}
    assert uni.universe_slugs("ashby") == {"new"}
    assert "old" in uni.universe_slugs("greenhouse") | {
        c.slug for c in uni.universe_companies("greenhouse", include_retired=True)}
