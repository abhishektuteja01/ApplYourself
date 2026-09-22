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


# --- the hot/cold split ----------------------------------------------------

TODAY = pd.Timestamp("2026-09-16")


def _co(slug, *, priority=False, kept_days_ago=None):
    return universe.UniverseCompany(
        name=slug.title(), ats="greenhouse", slug=slug, priority=priority,
        last_kept_at=None if kept_days_ago is None else TODAY - timedelta(days=kept_days_ago))


def _cursor():
    from src.discovery.crawl_cursor import CrawlCursor

    return CrawlCursor(ats="greenhouse")


def test_a_board_that_kept_a_row_recently_is_polled_every_run():
    companies = [_co("fresh", kept_days_ago=1), _co("edge", kept_days_ago=30),
                 _co("stale", kept_days_ago=31), _co("never")]

    sel = universe.select_for_run(companies, _cursor(), today=TODAY)

    # The window is inclusive: exactly HOT_WINDOW_DAYS ago still counts.
    assert {c.slug for c in sel.hot} == {"fresh", "edge"}
    assert {c.slug for c in sel.cold} == {"stale", "never"}


def test_a_watchlist_company_is_pinned_hot_however_barren():
    companies = [_co("watched", priority=True), _co("other")]

    sel = universe.select_for_run(companies, _cursor(), today=TODAY)

    assert [c.slug for c in sel.hot] == ["watched"]
    assert "watched" in {c.slug for c in sel.to_poll}


def test_the_cold_tail_is_fully_covered_in_one_rotation():
    """The property that makes the slice safe: no board is starved, and none
    is polled twice before every other has been polled once."""
    per_run = 10
    total = per_run * universe.COLD_ROTATION_RUNS
    companies = [_co(f"c{i:02d}") for i in range(total)]
    cursor = _cursor()

    visits = {}
    for run in range(universe.COLD_ROTATION_RUNS):
        sel = universe.select_for_run(companies, cursor, today=TODAY)
        assert len(sel.to_poll) == per_run
        for c in sel.to_poll:
            visits[c.slug] = visits.get(c.slug, 0) + 1
        cursor.advance(sel.cold, len(sel.to_poll), key=lambda c: c.slug, attr="cold_slug")

    assert len(visits) == total
    assert set(visits.values()) == {1}


def test_a_cold_list_shorter_than_the_rotation_still_advances():
    """Rounding down would give a slice of zero and poll nothing, forever."""
    companies = [_co("a"), _co("b")]
    cursor = _cursor()

    first = universe.select_for_run(companies, cursor, today=TODAY)
    assert len(first.to_poll) == 1
    cursor.advance(first.cold, 1, key=lambda c: c.slug, attr="cold_slug")
    second = universe.select_for_run(companies, cursor, today=TODAY)

    assert {c.slug for c in first.to_poll} != {c.slug for c in second.to_poll}


def test_the_two_rotations_do_not_seek_each_other():
    """Workday rotates `next_slug` over its tenants and the board lanes rotate
    `cold_slug` over their cold tail. One writer each."""
    cursor = _cursor()
    # 5 per run, so the "c9" below is always a real slug the rotation could hit.
    total = 5 * universe.COLD_ROTATION_RUNS
    companies = [_co(f"c{i}") for i in range(total)]
    slice_size = -(-total // universe.COLD_ROTATION_RUNS)

    cursor.next_slug = "c9"
    sel = universe.select_for_run(companies, cursor, today=TODAY)
    cursor.advance(sel.cold, len(sel.to_poll), key=lambda c: c.slug, attr="cold_slug")

    assert cursor.next_slug == "c9"
    assert cursor.cold_slug == f"c{slice_size}"


def test_last_kept_at_survives_a_round_trip_through_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text(
        "name,slug\nKept Co,kept\nEmpty Co,empty\n", encoding="utf-8")

    ledger = universe.HealthLedger("greenhouse", ["kept", "empty"])
    ledger.mark_ok("kept", 3)
    ledger.mark_ok("empty", 0)  # answered, but had nothing for us
    ledger.flush()

    by_slug = {c.slug: c for c in universe.load("greenhouse")}
    assert by_slug["kept"].last_kept_at == pd.Timestamp.today().normalize()
    assert by_slug["empty"].last_kept_at is None

    sel = universe.select_for_run(by_slug.values(), _cursor())
    assert [c.slug for c in sel.hot] == ["kept"]


def test_a_ledger_written_before_last_kept_at_existed_still_loads(tmp_path, monkeypatch):
    """The migration case: the parquet on disk predates the column, and the
    read, the update and the split all have to tolerate that."""
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "greenhouse.csv").write_text("name,slug\nOld Co,old\n", encoding="utf-8")

    old_columns = ["ats", "slug", "consecutive_404s", "last_ok", "last_yield", "pruned_at"]
    pd.DataFrame([{"ats": "greenhouse", "slug": "old", "consecutive_404s": 1,
                   "last_ok": pd.Timestamp("2026-09-01"), "last_yield": 4,
                   "pruned_at": pd.NaT}])[old_columns].to_parquet(
        universe.health_path("greenhouse"))

    loaded = universe.load("greenhouse")
    assert [c.last_kept_at for c in loaded] == [None]

    ledger = universe.HealthLedger("greenhouse", ["old"])
    ledger.mark_ok("old", 2)
    ledger.flush()

    df = pd.read_parquet(universe.health_path("greenhouse"))
    assert list(df.columns) == universe.HEALTH_COLUMNS
    assert df.loc[0, "last_kept_at"] == pd.Timestamp.today().normalize()
    # The strike counter came back as a number, not NaN + 1.
    assert df.loc[0, "consecutive_404s"] == 0


def _seed_universe(tmp_path, monkeypatch, slugs):
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "csv")
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    (tmp_path / "csv").mkdir(exist_ok=True)
    (tmp_path / "csv" / "greenhouse.csv").write_text(
        "name,slug\n" + "".join(f"Co {s},{s}\n" for s in slugs), encoding="utf-8")


def test_universe_slugs_includes_a_board_load_is_hiding(tmp_path, monkeypatch):
    """`load()` is a poll list, not a membership test."""
    _seed_universe(tmp_path, monkeypatch, ["alive", "cooling"])
    today = pd.Timestamp.today().normalize()
    pd.DataFrame([
        {"ats": "greenhouse", "slug": "alive", "consecutive_404s": 0,
         "last_ok": today, "last_yield": 1, "pruned_at": pd.NaT,
         "last_kept_at": today},
        {"ats": "greenhouse", "slug": "cooling", "consecutive_404s": 3,
         "last_ok": pd.NaT, "last_yield": 0, "pruned_at": today,
         "last_kept_at": pd.NaT},
    ]).to_parquet(universe.health_path("greenhouse"))

    assert {c.slug for c in universe.load("greenhouse")} == {"alive"}
    assert universe.universe_slugs("greenhouse") == {"alive", "cooling"}


def test_a_cooling_board_keeps_its_health_row_through_a_flush(tmp_path, monkeypatch):
    """The 14-day cooldown is self-erasing if the ledger is built from
    `load()`: the hidden row is an orphan, gets deleted, and with it the
    `pruned_at` and the strike count that retired the board."""
    slugs = [f"co{i:02d}" for i in range(14)]
    _seed_universe(tmp_path, monkeypatch, slugs)
    today = pd.Timestamp.today().normalize()
    rows = []
    for i, s in enumerate(slugs):
        cooling = i % 7 == 0  # two of the fourteen are mid-cooldown
        rows.append({"ats": "greenhouse", "slug": s,
                     "consecutive_404s": 3 if cooling else 0,
                     "last_ok": pd.NaT if cooling else today,
                     "last_yield": 0,
                     "pruned_at": today if cooling else pd.NaT,
                     "last_kept_at": pd.NaT})
    pd.DataFrame(rows).to_parquet(universe.health_path("greenhouse"))

    # One run's worth of marks: only the boards `load()` would hand the loop.
    ledger = universe.HealthLedger("greenhouse", universe.universe_slugs("greenhouse"))
    for c in universe.load("greenhouse")[:2]:
        ledger.mark_ok(c.slug, 0)
    ledger.flush()

    after = pd.read_parquet(universe.health_path("greenhouse"))
    assert len(after) == 14, "a board mid-cooldown lost its health row"
    cooled = after[after["pruned_at"].notna()]
    assert set(cooled["slug"]) == {"co00", "co07"}
    assert set(cooled["consecutive_404s"]) == {3}
