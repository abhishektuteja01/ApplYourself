"""scripts/dev/drop_filter_rows.py: the one-time removal of stored filter drops."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from tests.factories import scored_rows

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "drop_filter_rows.py"


def _load():
    spec = importlib.util.spec_from_file_location("drop_filter_rows", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_writes_nothing_and_apply_keeps_judged_rows(tmp_path, capsys):
    path = tmp_path / "scored.parquet"
    scored_rows(
        {"job_id": "aaaaaaaa", "scored_by_model": "score-judge:sonnet", "fit_score": 70.0},
        {"job_id": "bbbbbbbb", "scored_by_model": "filter:years"},
        {"job_id": "cccccccc", "scored_by_model": "filter:no-lane"},
    ).to_parquet(path, index=False)
    mod = _load()
    assert mod.main(["--dry-run", "--scored", str(path)]) == 0
    assert "2 filter rows of 3" in capsys.readouterr().out
    assert len(pd.read_parquet(path)) == 3
    assert mod.main(["--apply", "--scored", str(path)]) == 0
    left = pd.read_parquet(path)
    assert list(left["job_id"]) == ["aaaaaaaa"] and left["fit_score"].iloc[0] == 70.0
