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
