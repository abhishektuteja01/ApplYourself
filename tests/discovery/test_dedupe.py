"""Merge predicate, company alias ledger and sticky-id selection.

The failure these guard against is silent: a merge that picks a different
company spelling on the next run changes job_id, and every
pipeline/<job_id>/state.yaml and applications/<dir> for that role is orphaned.
"""
from __future__ import annotations

import importlib.util

import pandas as pd
import pytest
import yaml

from src import paths
from src.discovery import aliases, cleaning, dedupe

SEEDER = paths.REPO_ROOT / "scripts" / "seed_company_aliases.py"


def _load_seeder():
    spec = importlib.util.spec_from_file_location("seed_company_aliases", SEEDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(company: str, title: str = "AI Engineer", url: str = "", jd: str = "") -> dict:
    return {
        "company": company,
        "title": title,
        "url": url,
        "company_normalized": cleaning.normalize_company(company),
        "title_normalized": cleaning.normalize_title(title),
        "jd_text": jd,
    }


JD_A = "Build and ship retrieval pipelines for a clinical trials platform. " * 40
JD_B = JD_A + " Occasional travel required."
JD_OTHER = "Manage a warehouse team and own the shift schedule end to end. " * 40


# ---------- merge predicate ----------

def test_same_company_spellings_merge_on_jd_evidence():
    assert dedupe.same_posting(_row("Hilbert's AI", jd=JD_A), _row("Hilbert", jd=JD_B))


def test_squashed_company_key_merges_without_a_slug():
    # board_slug("") for a ?gh_jid= careers page: slug grouping cannot see this
    # pair, so the squashed key has to.
    a = _row("observeai", url="https://www.observe.ai/position?gh_jid=5383979008", jd=JD_A)
    b = _row("Observe.AI", jd=JD_B)
    assert dedupe.board_slug(a["url"]) == ""
    assert dedupe.same_posting(a, b)


def test_same_board_slug_merges_without_jd_text():
    a = _row("ON.energy", url="https://job-boards.greenhouse.io/onenergy/jobs/4384485009")
    b = _row("ONENERGY", url="https://job-boards.greenhouse.io/onenergy/jobs/4384485009?gh_src=x")
    assert dedupe.same_posting(a, b)


@pytest.mark.parametrize("a,b", [
    # Close company names, unrelated postings — the JD is the only evidence.
    (_row("sapien", url="https://jobs.ashbyhq.com/sapien/1", jd=JD_A),
     _row("Salient", url="https://jobs.ashbyhq.com/salient/2", jd=JD_OTHER)),
    # `solutions` is a parse failure; token_set_ratio scores it 100 against
    # every staffing agency with the word in its name.
    (_row("solutions", jd=JD_A), _row("Shakti Solutions", jd=JD_OTHER)),
    (_row("Federal Express Corporation", jd=JD_A), _row("American Express", jd=JD_OTHER)),
    (_row("New York Technology Partners", jd=JD_A), _row("EHW Partners", jd=JD_OTHER)),
])
def test_must_not_merge(a, b):
    assert not dedupe.same_posting(a, b)


def test_containment_alone_does_not_merge():
    assert not dedupe.same_posting(_row("apple", jd=JD_A), _row("applebees", jd=JD_B))


def test_no_jd_text_is_no_evidence():
    """Two spellings that would merge on JD do not merge without one."""
    assert not dedupe.same_posting(_row("Hilbert's AI"), _row("Hilbert"))


# ---------- alias ledger ----------

def test_canonical_prefers_the_board_name():
    chosen = aliases.choose_canonical([
        aliases.AliasCandidate("unlearn ai", "aggregator"),
        aliases.AliasCandidate("unlearn", "board"),
    ])
    assert chosen == "unlearn"


def test_canonical_tie_goes_to_first_seen():
    chosen = aliases.choose_canonical([
        aliases.AliasCandidate("clay", "aggregator"),
        aliases.AliasCandidate("clay labs", "aggregator"),
    ])
    assert chosen == "clay"


def test_source_of_truth_from_url():
    assert aliases.source_of_truth(
        "https://job-boards.greenhouse.io/unlearn/jobs/123"
    ) == "board"
    assert aliases.source_of_truth("https://www.linkedin.com/jobs/view/1") == "aggregator"
    assert aliases.source_of_truth("") == "aggregator"


def test_ledger_is_append_only(tmp_path):
    path = tmp_path / "company_aliases.parquet"
    day = pd.Timestamp("2026-06-01")
    ledger = aliases.append_aliases(
        aliases.empty_ledger(), [("hilbert", "hilbert s ai", "manual")], day
    )
    aliases.write_ledger(ledger, path)

    # A second run that now considers a different spelling best.
    ledger = aliases.append_aliases(
        aliases.load_ledger(path),
        [("hilbert", "hilbert inc", "board"), ("hilberts", "hilbert s ai", "board")],
        pd.Timestamp("2026-07-01"),
    )
    aliases.write_ledger(ledger, path)

    mapping = aliases.canonical_map(aliases.load_ledger(path))
    assert mapping["hilbert"] == "hilbert s ai"
    assert mapping["hilberts"] == "hilbert s ai"
    assert len(aliases.load_ledger(path)) == 2


def test_ledger_round_trips_the_schema(tmp_path):
    path = tmp_path / "company_aliases.parquet"
    aliases.write_ledger(
        aliases.append_aliases(
            aliases.empty_ledger(), [("clay", "clay labs", "board")],
            pd.Timestamp("2026-06-01"),
        ),
        path,
    )
    assert list(aliases.load_ledger(path).columns) == list(aliases.ALIAS_COLUMNS)


# ---------- sticky ids ----------

def _state(state: str, last_touch: str) -> dict:
    return {"state": state, "last_touch": last_touch}


def test_sticky_prefers_a_tracked_member():
    index = {"bbbb": _state("saved", "2026-06-01T00:00:00")}
    assert aliases.select_sticky_id(["aaaa", "bbbb"], index) == "bbbb"


def test_sticky_prefers_seen_when_none_is_tracked():
    assert aliases.select_sticky_id(["aaaa", "bbbb"], {}, seen_ids={"bbbb"}) == "bbbb"


def test_sticky_falls_back_to_the_survivor_order():
    assert aliases.select_sticky_id(["aaaa", "bbbb"], {}) == "aaaa"


@pytest.mark.parametrize("states,winner", [
    ({"a": "applied", "b": "skip"}, "a"),
    ({"a": "skip", "b": "applied"}, "b"),
    ({"a": "saved", "b": "skip"}, "a"),
    ({"a": "tailored", "b": "saved"}, "a"),
    ({"a": "applied", "b": "tailored"}, "a"),
])
def test_sticky_two_tracked_resolves_by_state_precedence(states, winner):
    index = {k: _state(v, "2026-06-01T00:00:00") for k, v in states.items()}
    assert aliases.select_sticky_id(["a", "b"], index) == winner


def test_sticky_equal_states_resolve_by_last_touch():
    index = {
        "a": _state("applied", "2026-06-01T00:00:00"),
        "b": _state("applied", "2026-06-05T00:00:00"),
    }
    assert aliases.select_sticky_id(["a", "b"], index) == "b"
    assert aliases.select_sticky_id(["b", "a"], index) == "b"


# ---------- the seed migration ----------

def _write_state(pipeline_dir, job_id, company, title, state, url="", at="2026-06-01T00:00:00"):
    d = pipeline_dir / job_id
    d.mkdir(parents=True)
    (d / "state.yaml").write_text(yaml.safe_dump({
        "job_id": job_id,
        "company": company,
        "title": title,
        "url": url,
        "state": state,
        "state_history": [{"state": state, "at": at, "note": ""}],
        "last_touch": at,
    }), encoding="utf-8")


def _seed_tree(tmp_path):
    """Two spellings of one company plus an unrelated role, with real ids."""
    pipeline_dir = tmp_path / "pipeline"
    ids = {}
    for company, state, url, at in [
        ("Unlearn.AI", "applied", "", "2026-06-05T00:00:00"),
        ("Unlearn", "applied", "https://job-boards.greenhouse.io/unlearn/jobs/1",
         "2026-06-01T00:00:00"),
        ("Acme Robotics", "saved", "", "2026-06-01T00:00:00"),
    ]:
        job_id = cleaning.compute_job_id(
            cleaning.normalize_company(company), cleaning.normalize_title("AI Engineer")
        )
        ids[company] = job_id
        _write_state(pipeline_dir, job_id, company, "AI Engineer", state, url, at)

    clean = tmp_path / "clean.parquet"
    pd.DataFrame({
        "job_id": [ids["Unlearn.AI"], ids["Unlearn"], ids["Acme Robotics"]],
        "jd_text": [JD_A, JD_B, JD_OTHER],
    }).to_parquet(clean, index=False)
    return pipeline_dir, clean, ids


def test_seed_plans_one_group_and_keeps_the_later_touched_id(tmp_path):
    seeder = _load_seeder()
    pipeline_dir, clean, ids = _seed_tree(tmp_path)

    records, drifted = seeder.load_records(pipeline_dir, clean)
    assert drifted == []
    assert len(records) == 3

    groups = seeder.merge_groups(records)
    assert len(groups) == 1

    index = {r["job_id"]: r for r in records}
    plan = seeder.plan_group(groups[0], index)
    # Board display name feeds the canonical; both members are applied, so the
    # sticky id is the one touched most recently.
    assert plan["canonical"] == "unlearn"
    assert plan["survivor"] == ids["Unlearn.AI"]
    assert [r["job_id"] for r in plan["losers"]] == [ids["Unlearn"]]


def test_seed_writes_the_ledger_and_dry_run_does_not(tmp_path, monkeypatch, capsys):
    seeder = _load_seeder()
    pipeline_dir, clean, _ = _seed_tree(tmp_path)
    out = tmp_path / "company_aliases.parquet"
    expected = tmp_path / "expected.txt"
    expected.write_text("", encoding="utf-8")
    argv = ["seed", "--pipeline-dir", str(pipeline_dir), "--clean", str(clean),
            "--out", str(out), "--expected", str(expected)]

    monkeypatch.setattr("sys.argv", argv + ["--dry-run"])
    assert seeder.main() == 1          # one unexpected change, nothing reviewed yet
    assert not out.exists()

    monkeypatch.setattr("sys.argv", argv)
    assert seeder.main() == 0
    mapping = aliases.canonical_map(aliases.load_ledger(out))
    assert mapping == {"unlearn ai": "unlearn", "unlearn": "unlearn"}


def test_seed_gate_passes_when_the_change_is_reviewed(tmp_path, monkeypatch):
    seeder = _load_seeder()
    pipeline_dir, clean, ids = _seed_tree(tmp_path)
    expected = tmp_path / "expected.txt"
    expected.write_text(
        f"# reviewed\n{ids['Unlearn.AI']} {ids['Unlearn']}\n", encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "seed", "--pipeline-dir", str(pipeline_dir), "--clean", str(clean),
        "--out", str(tmp_path / "x.parquet"), "--expected", str(expected), "--dry-run",
    ])
    assert seeder.main() == 0


def test_seed_reports_drift_separately(tmp_path):
    seeder = _load_seeder()
    pipeline_dir = tmp_path / "pipeline"
    _write_state(pipeline_dir, "deadbeef", "Mingledorff's", "SAP Business Analyst", "applied")

    records, drifted = seeder.load_records(pipeline_dir, tmp_path / "missing.parquet")
    assert records == []
    assert [r["job_id"] for r in drifted] == ["deadbeef"]


def test_seed_worklist_skips_already_closed_losers(tmp_path):
    seeder = _load_seeder()
    pipeline_dir, clean, ids = _seed_tree(tmp_path)
    records, _ = seeder.load_records(pipeline_dir, clean)
    index = {r["job_id"]: r for r in records}
    plans = [seeder.plan_group(g, index) for g in seeder.merge_groups(records)]

    lines = seeder.track_worklist(plans)
    assert lines == [
        f'uv run track {ids["Unlearn"]} withdrawn '
        f'--note "duplicate of {ids["Unlearn.AI"]}"'
    ]

    for plan in plans:
        plan["losers"][0]["state"] = "skip"
    assert all(line.startswith("#") for line in seeder.track_worklist(plans))


def test_seed_never_writes_a_state_yaml(tmp_path, monkeypatch):
    """R10: /track is the sole writer of state.yaml."""
    seeder = _load_seeder()
    pipeline_dir, clean, _ = _seed_tree(tmp_path)
    before = {p: p.read_bytes() for p in pipeline_dir.rglob("state.yaml")}

    monkeypatch.setattr("sys.argv", [
        "seed", "--pipeline-dir", str(pipeline_dir), "--clean", str(clean),
        "--out", str(tmp_path / "out.parquet"), "--expected", str(tmp_path / "none.txt"),
    ])
    assert seeder.main() == 0
    assert {p: p.read_bytes() for p in pipeline_dir.rglob("state.yaml")} == before


# ---------- blocking over a frame ----------

def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["job_id"] = [
        cleaning.compute_job_id(c, t)
        for c, t in zip(df["company_normalized"], df["title_normalized"])
    ]
    for column, default in (("location", ""), ("_not_applyable", True),
                            ("_loc_unresolved", False)):
        if column not in df.columns:
            df[column] = default
    return df


def _buckets(df: pd.DataFrame) -> set[frozenset]:
    return {frozenset(b) for b in dedupe.block(df)}


def test_block_pairs_two_spellings_of_one_company():
    df = _frame([_row("Hilbert", jd=JD_A), _row("Hilbert's AI", jd=JD_B)])
    assert frozenset([0, 1]) in _buckets(df)


def test_block_pairs_on_the_board_slug_alone():
    df = _frame([
        _row("Hilbert", title="AI Engineer - Core",
             url="https://jobs.ashbyhq.com/hilberts/aaa"),
        _row("Something Else", title="AI Engineer Core",
             url="https://jobs.ashbyhq.com/hilberts/bbb"),
    ])
    assert frozenset([0, 1]) in _buckets(df)


def test_block_ignores_a_blank_url_and_a_blank_company():
    df = _frame([_row("", title="AI Engineer"), _row("", title="Data Engineer")])
    assert _buckets(df) == set()


def test_block_containment_needs_four_characters():
    """A two-letter squashed key sits inside unrelated names — `hp` is inside
    `graphpad` — so indexing it would bucket them together."""
    df = _frame([
        _row("HP", title="AI Engineer"),
        _row("Graphpad", title="Data Engineer"),
    ])
    assert _buckets(df) == set()


def test_block_finds_containment_that_is_not_a_prefix():
    df = _frame([_row("Cigna", jd=JD_A), _row("The Cigna Group", jd=JD_B)])
    assert any({0, 1} <= b for b in _buckets(df))


# ---------- resolve: survivors, sticky ids, merge records ----------

def test_resolve_keeps_one_row_per_posting():
    out, merges = dedupe.resolve(_frame([
        _row("Hilbert", jd=JD_A),
        _row("Hilbert's AI", jd=JD_B),
        _row("Warehouse Co", title="Shift Lead", jd=JD_OTHER),
    ]))
    assert len(out) == 2
    assert len(merges) == 1
    assert sorted(merges[0]["variants"]) == ["hilbert", "hilbert s ai"]


def test_resolve_never_merges_two_staffing_agencies_sharing_a_client_jd():
    """Confirmed non-duplicates: you apply through one agency or the other."""
    out, _ = dedupe.resolve(_frame([
        _row("Quadrant IQ Solutions LLC", jd=JD_A),
        _row("BayOne Solutions", jd=JD_A),
    ]))
    assert len(out) == 2


def test_resolve_survivor_prefers_an_applyable_url_over_a_longer_jd():
    df = _frame([
        {**_row("Hilbert", url="https://www.linkedin.com/jobs/view/1", jd=JD_A + "x" * 500),
         "_not_applyable": True},
        {**_row("Hilbert's AI", url="https://jobs.ashbyhq.com/hilberts/a", jd=JD_B),
         "_not_applyable": False},
    ])
    out, _ = dedupe.resolve(df)
    assert out.iloc[0]["url"] == "https://jobs.ashbyhq.com/hilberts/a"


def test_resolve_survivor_prefers_a_resolved_location_over_a_longer_jd():
    df = _frame([
        {**_row("Hilbert", jd=JD_A + "x" * 500), "location": "Remote",
         "_loc_unresolved": True},
        {**_row("Hilbert's AI", jd=JD_B), "location": "Austin, TX",
         "_loc_unresolved": False},
    ])
    out, _ = dedupe.resolve(df)
    assert out.iloc[0]["location"] == "Austin, TX"


def test_resolve_takes_the_tracked_members_id_not_the_survivors():
    df = _frame([_row("Hilbert's AI", jd=JD_A), _row("Hilbert", jd=JD_B)])
    tracked = df.iloc[1]["job_id"]
    out, merges = dedupe.resolve(
        df, state_index={tracked: {"state": "applied", "last_touch": "2026-06-01"}}
    )
    assert out.iloc[0]["job_id"] == tracked
    assert merges[0]["job_id"] == tracked


def test_resolve_falls_back_to_a_seen_member():
    df = _frame([_row("Hilbert's AI", jd=JD_A), _row("Hilbert", jd=JD_B)])
    seen = df.iloc[1]["job_id"]
    out, _ = dedupe.resolve(df, seen_ids={seen})
    assert out.iloc[0]["job_id"] == seen


def test_resolve_honours_a_canonical_already_pinned_in_the_ledger():
    df = _frame([_row("Hilbert's AI", jd=JD_A), _row("Hilbert", jd=JD_B)])
    _, merges = dedupe.resolve(df, canonical={"hilbert s ai": "hilbert"})
    assert merges[0]["canonical"] == "hilbert"


def test_resolve_sticky_id_is_always_a_member_of_the_group():
    df = _frame([_row("Hilbert", jd=JD_A), _row("Hilbert's AI", jd=JD_B)])
    out, merges = dedupe.resolve(df)
    assert merges[0]["job_id"] in set(df["job_id"])
    assert out.iloc[0]["job_id"] in set(df["job_id"])


def test_resolve_survives_a_window_with_repeated_index_labels():
    """The streamed window concatenates shards, so its index repeats. Duplicate
    labels silently collapsed the union-find parent map."""
    df = pd.concat([
        _frame([_row("Hilbert", jd=JD_A)]),
        _frame([_row("Warehouse Co", title="Shift Lead", jd=JD_OTHER)]),
    ])
    assert list(df.index) == [0, 0]
    out, merges = dedupe.resolve(df)
    assert len(out) == 2 and merges == []


# ---------- D5.7: multi-city postings ----------

def test_resolve_records_every_city_in_a_multi_city_group():
    rows = [
        {**_row("Accenture", url=f"https://example.com/{i}", jd=JD_A), "location": city}
        for i, city in enumerate(
            ["Albany, NY", "Arlington, VA", "Atlanta, GA", "Austin, TX", "Boston, MA"]
        )
    ]
    out, _ = dedupe.resolve(_frame(rows))
    assert len(out) == 1
    assert out.iloc[0]["location_count"] == 5
    assert out.iloc[0]["all_locations"] == (
        "Albany, NY | Arlington, VA | Atlanta, GA | Austin, TX | Boston, MA"
    )


def test_all_locations_is_capped():
    rows = [
        {**_row("Accenture", url=f"https://example.com/{i}", jd=JD_A),
         "location": f"City {i:02d}, ST"}
        for i in range(15)
    ]
    out, _ = dedupe.resolve(_frame(rows))
    assert out.iloc[0]["location_count"] == 15
    assert len(out.iloc[0]["all_locations"].split(" | ")) == dedupe.LOCATION_CAP


def test_a_single_row_still_reports_its_own_location():
    out, _ = dedupe.resolve(_frame([{**_row("Acme"), "location": "Austin, TX"}]))
    assert (out.iloc[0]["location_count"], out.iloc[0]["all_locations"]) == (1, "Austin, TX")


# ---------- the level guard ----------

@pytest.mark.parametrize("title_a,title_b", [
    ("Machine Learning Engineer I", "Machine Learning Engineer II"),
    ("Data Analyst", "Data Analyst Intern"),
    ("Data Engineer", "Lead Data Engineer"),
])
def test_two_levels_of_one_role_never_merge_on_jd_evidence(title_a, title_b):
    """Level-numbered siblings routinely share the same boilerplate JD, so the
    evidence rule alone would delete a real posting."""
    assert dedupe.same_posting(
        _row("Acme", title=title_a, jd=JD_A), _row("Acme", title=title_b, jd=JD_A)
    ) is False


def test_an_identical_url_still_merges_two_levels():
    """The url is the company's own statement that these are one posting."""
    url = "https://jobs.ashbyhq.com/acme/aaa"
    assert dedupe.same_posting(
        _row("Acme", title="Data Analyst", url=url, jd=JD_A),
        _row("Acme", title="Data Analyst Intern", url=url, jd=JD_OTHER),
    ) is True


def test_the_same_company_and_title_merge_whatever_the_jd_says():
    """job_id hashes exactly this pair, so letting both through would write one
    job_id twice."""
    assert dedupe.same_posting(
        _row("Acme", title="AI Engineer", jd=JD_A),
        _row("Acme", title="AI Engineer", jd=JD_OTHER),
    ) is True


# ---------- golden: the whole cleaning run, exact surviving job_id set ----------

GOLDEN_ROWS = [
    # Two spellings of one company, board row second and shorter. Merges on
    # jd evidence; the board url must be the survivor.
    ("Hilbert", "AI Engineer - Core", "https://www.linkedin.com/jobs/view/1",
     "Austin, TX", JD_A + "x" * 400),
    ("Hilbert's AI", "AI Engineer Core", "https://jobs.ashbyhq.com/hilberts/a",
     "Austin, TX", JD_B),
    # Punctuation-only variants: the squashed key merges them.
    ("Observe.AI", "Data Engineer", "https://job-boards.greenhouse.io/observeai/jobs/1",
     "Austin, TX", JD_A),
    ("observeai", "Data Engineer", "https://www.indeed.com/viewjob?jk=2",
     "Austin, TX", JD_B),
    # Two staffing agencies reposting one client JD — must both survive.
    ("Quadrant IQ Solutions LLC", "ML Engineer", "https://example.com/q",
     "Austin, TX", JD_A),
    ("BayOne Solutions", "ML Engineer", "https://example.com/b",
     "Austin, TX", JD_A),
    # A level-numbered sibling pair sharing boilerplate — both real roles.
    ("Acme", "Machine Learning Engineer I", "https://example.com/i",
     "Austin, TX", JD_A),
    ("Acme", "Machine Learning Engineer II", "https://example.com/ii",
     "Austin, TX", JD_A),
    # Containment that is not a duplicate.
    ("Apple", "Backend Engineer", "https://example.com/apple",
     "Austin, TX", JD_A),
    ("Applebees", "Backend Engineer", "https://example.com/applebees",
     "Austin, TX", JD_OTHER),
    # One posting listed in five cities.
    *[
        ("Accenture", "Cloud Architect", f"https://example.com/acc{i}", city, JD_A)
        for i, city in enumerate(
            ["Albany, NY", "Arlington, VA", "Atlanta, GA", "Austin, TX", "Boston, MA"]
        )
    ],
]


def _golden_raw(raw_dir):
    from src.discovery.schema import make_row

    raw_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        make_row(site="linkedin", company=company, title=title, job_url=url,
                 location=location, description=jd,
                 date_posted=pd.Timestamp("2026-06-01"),
                 scraped_date=pd.Timestamp("2026-06-06"),
                 ingested_run_id="2026-06-06_1000")
        for company, title, url, location, jd in GOLDEN_ROWS
    ]
    pd.DataFrame(rows).to_parquet(raw_dir / "2026-06-06_1000.parquet", index=False)


def test_golden_surviving_job_ids(tmp_path, monkeypatch):
    """The whole cleaning run over a fixed row set. A change to blocking, the
    merge predicate or the survivor tie-break that moves any of these ids is a
    change to which pipeline/<job_id>/state.yaml a role keeps."""
    from src.discovery.config import DiscoveryConfig, LocationAllowlist

    monkeypatch.setattr(
        cleaning, "load_config",
        lambda *a, **k: DiscoveryConfig(
            location_allowlist=LocationAllowlist(countries=["United States"])
        ),
    )
    _golden_raw(tmp_path / "jobs" / "raw")
    df = cleaning.run(
        run_id="2026-06-06_1000",
        raw_dir=tmp_path / "jobs" / "raw",
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    survivors = {
        row["company"]: (row["job_id"], row["url"], row["location_count"])
        for _, row in df.iterrows()
    }
    assert set(survivors) == {
        "Hilbert's AI",            # board url beat the longer aggregator jd
        "Observe.AI",
        "Quadrant IQ Solutions LLC", "BayOne Solutions",
        "Acme",                    # one row per level
        "Apple", "Applebees",
        "Accenture",
    }
    assert len(df) == 9
    assert survivors["Hilbert's AI"][1] == "https://jobs.ashbyhq.com/hilberts/a"
    assert survivors["Accenture"][2] == 5
    # The ids themselves, so a silent re-hash fails here and not in
    # production. Untracked and unseen, so each group lands on the id of its
    # canonical spelling — the board's, not the aggregator's.
    assert survivors["Hilbert's AI"][0] == cleaning.compute_job_id(
        "hilbert s ai", "ai engineer core")
    assert survivors["Observe.AI"][0] == cleaning.compute_job_id(
        "observe ai", "data engineer")


def test_golden_merges_are_pinned_in_the_alias_ledger(tmp_path, monkeypatch):
    from src.discovery.config import DiscoveryConfig, LocationAllowlist

    monkeypatch.setattr(
        cleaning, "load_config",
        lambda *a, **k: DiscoveryConfig(
            location_allowlist=LocationAllowlist(countries=["United States"])
        ),
    )
    _golden_raw(tmp_path / "jobs" / "raw")
    kwargs = dict(
        raw_dir=tmp_path / "jobs" / "raw",
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    first = cleaning.run(run_id="2026-06-06_1000", **kwargs)
    ledger = pd.read_parquet(tmp_path / "jobs" / "company_aliases.parquet")
    assert set(ledger["variant_normalized"]) >= {"hilbert", "hilbert s ai"}
    assert ledger["variant_normalized"].is_unique

    # Re-running must not move an id or rewrite a pinned canonical.
    second = cleaning.run(run_id="2026-06-06_1100", **kwargs)
    assert set(second["job_id"]) == set(first["job_id"])
    after = pd.read_parquet(tmp_path / "jobs" / "company_aliases.parquet")
    pd.testing.assert_frame_equal(ledger, after)
