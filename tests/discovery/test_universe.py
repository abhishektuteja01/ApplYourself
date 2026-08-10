import pytest
import pandas as pd
from datetime import timedelta
from src.discovery import universe
from src.discovery.universe import UniverseCompany

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

def test_universe_health_ledger_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)

    # Success
    universe.update_health("greenhouse", "acme", success=True, rows=5)
    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert len(df) == 1
    row = df.iloc[0]
    assert row["consecutive_404s"] == 0
    assert row["last_yield"] == 5
    assert pd.isna(row["pruned_at"])

    # 404 x 2
    universe.update_health("greenhouse", "acme", success=False)
    universe.update_health("greenhouse", "acme", success=False)
    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert df.iloc[0]["consecutive_404s"] == 2
    assert pd.isna(df.iloc[0]["pruned_at"])

    # 404 x 3 -> pruned
    universe.update_health("greenhouse", "acme", success=False)
    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert df.iloc[0]["consecutive_404s"] == 3
    assert not pd.isna(df.iloc[0]["pruned_at"])

    # Success -> reset
    universe.update_health("greenhouse", "acme", success=True, rows=2)
    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert df.iloc[0]["consecutive_404s"] == 0
    assert df.iloc[0]["last_yield"] == 2
    assert pd.isna(df.iloc[0]["pruned_at"])

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
