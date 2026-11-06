"""uv run doctor lists every kind of disagreement and writes nothing."""

from __future__ import annotations

import json

import pandas as pd

from applyourself import doctor
from applyourself.shared import paths
from tests.factories import write_state


def test_each_kind_of_mismatch_is_listed_and_nothing_is_written(capsys):
    lane = paths.APPLICATIONS / "lane"
    (lane / "2026-09-01_a_x_aaaaaaaa").mkdir(parents=True)  # no state
    (lane / "2026-09-01_b_x_bbbbbbbb").mkdir(parents=True)
    write_state(paths.PIPELINE, "bbbbbbbb", "tailored", tailored_dirs=["lane/gone_bbbbbbbb"])
    write_state(paths.PIPELINE, "cccccccc", "saved")
    paths.APPLY_RUNS.mkdir(parents=True)
    (paths.APPLY_RUNS / "submitted.jsonl").write_text(
        "".join(
            json.dumps({"job_id": j, "status": "accepted"}) + "\n" for j in ("cccccccc", "dddddddd")
        ),
        encoding="utf-8",
    )
    paths.STORE.parent.mkdir(parents=True)
    pd.DataFrame({"job_id": ["bbbbbbbb"]}).to_parquet(paths.STORE)
    before = sorted(p for p in paths.root().rglob("*"))
    assert doctor.main([]) == 1
    assert sorted(p for p in paths.root().rglob("*")) == before
    assert capsys.readouterr().out.splitlines() == [
        "dir_missing bbbbbbbb lane/gone_bbbbbbbb",
        "folder_no_state aaaaaaaa lane/2026-09-01_a_x_aaaaaaaa",
        "ledger_no_state dddddddd",
        "not_in_store cccccccc",
        "sent_not_applied cccccccc state saved",
        "5 mismatch(es)",
    ]
