import pandas as pd
from datetime import timedelta
from src.discovery import universe

def test_universe_priority_ordering(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)

    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text("name,slug,extra\nCsv Only,csv-only,\nYielding Co,yielding,\n", encoding="utf-8")

    # Write health
    health_df = pd.DataFrame([
        {"ats": "greenhouse", "slug": "yielding", "consecutive_404s": 0, "last_ok": pd.Timestamp.today(), "last_yield": 5, "pruned_at": None},
        {"ats": "greenhouse", "slug": "csv-only", "consecutive_404s": 0, "last_ok": None, "last_yield": 0, "pruned_at": None},
    ])
    health_df.to_parquet(universe.health_path("greenhouse"))

    # Write companies.yaml
    (tmp_path / "companies.yaml").write_text("""\
schema_version: 1
companies:
  - name: Watchlist Co
    ats: greenhouse
    slug: watchlist
""", encoding="utf-8")

    res = universe.load("greenhouse")
    assert len(res) == 3
    assert res[0].slug == "watchlist"
    assert res[0].priority is True

    assert res[1].slug == "yielding"
    assert res[1].priority is False

    assert res[2].slug == "csv-only"

def test_universe_local_csv_merges_with_tracked(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text(
        "name,slug\nTracked Co,tracked\nCurated Name,shared\n", encoding="utf-8")
    (tmp_path / "csv" / "greenhouse.local.csv").write_text(
        "name,slug\nLocal Co,local\nBulk Name,shared\n", encoding="utf-8")

    res = {c.slug: c for c in universe.load("greenhouse")}
    assert set(res) == {"tracked", "local", "shared"}
    # Tracked loads second, so its name wins on the overlapping slug.
    assert res["shared"].name == "Curated Name"
    assert all(c.priority is False for c in res.values())


def test_universe_local_csv_absent_is_fine(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text("name,slug\nTracked Co,tracked\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        res = universe.load("greenhouse")
    assert [c.slug for c in res] == ["tracked"]
    assert "local.csv" not in caplog.text


def test_universe_dedupe_watchlist_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text("name,slug,extra\nCsv Co,acme,\n", encoding="utf-8")

    (tmp_path / "companies.yaml").write_text("""\
schema_version: 1
companies:
  - name: Watchlist Co
    ats: greenhouse
    slug: acme
""", encoding="utf-8")
    res = universe.load("greenhouse")
    assert len(res) == 1
    assert res[0].name == "Watchlist Co"
    assert res[0].priority is True

def _run(ats, slug, success, rows=0, known=None):
    """One lane's run: mark one slug, flush, return the ledger frame."""
    ledger = universe.HealthLedger(ats, known if known is not None else [slug])
    if success:
        ledger.mark_ok(slug, rows)
    else:
        ledger.mark_dead(slug)
    ledger.flush()
    return pd.read_parquet(universe.health_path(ats))


def test_universe_health_ledger_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)

    # Success
    df = _run("greenhouse", "acme", True, rows=5)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["consecutive_404s"] == 0
    assert row["last_yield"] == 5
    assert pd.isna(row["pruned_at"])

    # 404 x 2
    _run("greenhouse", "acme", False)
    df = _run("greenhouse", "acme", False)
    assert df.iloc[0]["consecutive_404s"] == 2
    assert pd.isna(df.iloc[0]["pruned_at"])

    # 404 x 3 -> pruned
    df = _run("greenhouse", "acme", False)
    assert df.iloc[0]["consecutive_404s"] == 3
    assert not pd.isna(df.iloc[0]["pruned_at"])

    # Success -> reset
    df = _run("greenhouse", "acme", True, rows=2)
    assert df.iloc[0]["consecutive_404s"] == 0
    assert df.iloc[0]["last_yield"] == 2
    assert pd.isna(df.iloc[0]["pruned_at"])


def test_health_ledger_writes_once_for_n_updates(tmp_path, monkeypatch):
    """The whole point of batching: one file rewrite per lane, not per company."""
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    writes = []
    monkeypatch.setattr(universe, "write_parquet",
                        lambda df, path: writes.append(path))

    ledger = universe.HealthLedger("greenhouse", [f"co{i}" for i in range(50)])
    for i in range(50):
        ledger.mark_ok(f"co{i}", i)
    ledger.mark_dead("co7")
    ledger.flush()

    assert len(writes) == 1


def test_health_ledger_flush_on_deadline_break_is_not_double_written(tmp_path, monkeypatch):
    """`fetch` flushes on the deadline break AND at the end; the second is a
    no-op, so a truncated run keeps its health at the cost of one write."""
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    ledger = universe.HealthLedger("greenhouse", ["acme", "beta"])
    ledger.mark_ok("acme", 3)
    ledger.flush()          # deadline break

    writes = []
    monkeypatch.setattr(universe, "write_parquet",
                        lambda df, path: writes.append(path))
    ledger.flush()          # end of fetch
    assert writes == []

    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert list(df["slug"]) == ["acme"]
    assert df.iloc[0]["last_yield"] == 3


def test_health_ledger_flush_drops_orphans_and_keeps_current_slugs(tmp_path, monkeypatch):
    """S2.4: slugs that left the CSVs and the watchlist were carried forever."""
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    pd.DataFrame([
        {"ats": "greenhouse", "slug": "still-here", "consecutive_404s": 1,
         "last_ok": pd.Timestamp("2024-01-01"), "last_yield": 4, "pruned_at": None},
        {"ats": "greenhouse", "slug": "orphan-a", "consecutive_404s": 0,
         "last_ok": None, "last_yield": 0, "pruned_at": None},
        {"ats": "greenhouse", "slug": "orphan-b", "consecutive_404s": 2,
         "last_ok": None, "last_yield": 0, "pruned_at": None},
    ]).to_parquet(universe.health_path("greenhouse"))

    ledger = universe.HealthLedger("greenhouse", ["still-here", "new-co"])
    ledger.mark_ok("new-co", 1)
    ledger.flush()

    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert sorted(df["slug"]) == ["new-co", "still-here"]
    # An untouched survivor keeps its history.
    assert df.set_index("slug").at["still-here", "consecutive_404s"] == 1


def test_health_ledger_prunes_orphans_with_no_updates_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    pd.DataFrame([
        {"ats": "greenhouse", "slug": "orphan", "consecutive_404s": 0,
         "last_ok": None, "last_yield": 0, "pruned_at": None},
    ]).to_parquet(universe.health_path("greenhouse"))

    universe.HealthLedger("greenhouse", ["acme"]).flush()
    assert pd.read_parquet(universe.health_path("greenhouse")).empty


def test_health_ledger_empty_lane_writes_no_file(tmp_path, monkeypatch):
    """A lane that polled nothing must not create an empty ledger."""
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    universe.HealthLedger("greenhouse", ["acme"]).flush()
    assert not universe.health_path("greenhouse").exists()


def test_universe_load_skips_pruned_unless_14_days(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text("name,slug,extra\nA,recent-pruned,\nB,old-pruned,\n", encoding="utf-8")

    today = pd.Timestamp.today().normalize()
    health_df = pd.DataFrame([
        {"ats": "greenhouse", "slug": "recent-pruned", "consecutive_404s": 3, "last_ok": None, "last_yield": 0, "pruned_at": today},
        {"ats": "greenhouse", "slug": "old-pruned", "consecutive_404s": 3, "last_ok": None, "last_yield": 0, "pruned_at": today - timedelta(days=15)},
    ])
    health_df.to_parquet(universe.health_path("greenhouse"))

    res = universe.load("greenhouse")
    slugs = [c.slug for c in res]
    assert "recent-pruned" not in slugs
    assert "old-pruned" in slugs

def test_universe_unsupported_ats_skipped(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir()

    (tmp_path / "companies.yaml").write_text("""\
schema_version: 1
companies:
  - name: Watchlist Co
    ats: icims
    slug: acme
""", encoding="utf-8")
    res = universe.load("icims")
    assert len(res) == 0
    assert "unsupported ats" in caplog.text.lower()

def test_universe_empty_csv_falls_back_to_watchlist(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text("", encoding="utf-8")  # Empty

    (tmp_path / "companies.yaml").write_text("""\
schema_version: 1
companies:
  - name: Watchlist Co
    ats: greenhouse
    slug: acme
""", encoding="utf-8")
    res = universe.load("greenhouse")
    assert len(res) == 1

def test_universe_empty_name_or_slug_skipped(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text("name,slug,extra\n,slug1,\nName2,,\nName3,slug3,\n", encoding="utf-8")

    res = universe.load("greenhouse")
    assert len(res) == 1
    assert res[0].slug == "slug3"
    assert "empty name or slug" in caplog.text.lower()
