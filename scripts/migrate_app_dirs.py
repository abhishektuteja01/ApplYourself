"""One-off: bring open roles' folders to the current layout.

For the latest folder (tailored_dirs[-1]) of every role in `saved` or
`tailored`:

  1. jd_snapshot.md front matter is rewritten as valid YAML and gains
     `keywords_to_mirror` from scored.parquet.
  2. company_answers.md becomes company_answers.yaml (INSUFFICIENT_RESEARCH in
     any spelling -> null), then goes through `uv run app build answers`. A key
     that fails the check is set to null, so /apply parks that question rather
     than send an unchecked answer; the .md stays beside it for reference.

--prune-empty also removes empty folders no state.yaml points at.

Dry run by default; --apply writes. Never opens a state.yaml for writing.

  uv run python scripts/migrate_app_dirs.py [--apply] [--prune-empty]
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import app_cli, paths, state_io  # noqa: E402

OPEN = {"saved", "tailored"}
_TOKEN = re.compile(r"^\W*insufficient[_ ]research\W*$", re.IGNORECASE)


def split_front_matter(text: str) -> tuple[dict, str]:
    """The old snapshot wrote `key: value` lines unquoted, which is not always
    valid YAML, so read them line by line."""
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    meta = {}
    for line in head.splitlines():
        key, sep, val = line.partition(":")
        if sep:
            meta[key.strip()] = val.strip()
    try:
        parsed = yaml.safe_load(head)
        if isinstance(parsed, dict):
            meta.update({k: v for k, v in parsed.items() if isinstance(v, list)})
    except yaml.YAMLError:
        pass
    return meta, body.lstrip("\n")


def parse_answers_md(text: str) -> dict:
    meta = dict(re.findall(r"^(researched_at|company|job_id):\s*(.*)$", text, re.M))
    parts = re.split(r"^## (.+)$", text, flags=re.M)
    sections = {h.strip(): b.strip() for h, b in zip(parts[1::2], parts[2::2])}
    out = {"researched_at": meta.get("researched_at", ""), "sources": [], "uses": []}
    for key in app_cli.ANSWER_KEYS:
        v = sections.get(key, "").strip()
        out[key] = None if not v or _TOKEN.match(v) else " ".join(v.split())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--prune-empty", action="store_true")
    args = ap.parse_args(argv)
    act = args.apply
    scored = pd.read_parquet(paths.SCORED).set_index("job_id") if paths.SCORED.exists() else None
    index = state_io.load_state_index(paths.PIPELINE)
    report: dict[str, list] = {"snapshot": [], "answers": [], "answers_nulled": [], "missing_dir": [], "pruned": []}

    for job_id, st in sorted(index.items()):
        if st.get("state") not in OPEN or not st.get("tailored_dirs"):
            continue
        folder = paths.APPLICATIONS / st["tailored_dirs"][-1]
        if not folder.is_dir():
            report["missing_dir"].append(job_id)
            continue

        snap = folder / app_cli.SNAPSHOT
        if snap.is_file():
            meta, body = split_front_matter(snap.read_text(encoding="utf-8"))
            if "keywords_to_mirror" not in meta or not isinstance(meta["keywords_to_mirror"], list):
                kws = []
                if scored is not None and job_id in scored.index:
                    kws = [str(k) for k in list(scored.loc[job_id, "keywords_to_mirror"] if not isinstance(
                        scored.loc[job_id], pd.DataFrame) else scored.loc[job_id].iloc[0]["keywords_to_mirror"]) or []]
                meta["keywords_to_mirror"] = kws
                report["snapshot"].append(job_id)
                if act:
                    snap.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
                                    + "---\n\n" + body, encoding="utf-8")

        md = folder / "company_answers.md"
        target = folder / app_cli.ANSWERS
        if md.is_file() and not target.exists():
            data = parse_answers_md(md.read_text(encoding="utf-8"))
            report["answers"].append(job_id)
            if act:
                target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000),
                                  encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    out, code = app_cli.cmd_build_answers(job_id)
                if code == 2:
                    bad = sorted({p["where"] for p in out["problems"]} & set(app_cli.ANSWER_KEYS))
                    for key in bad:
                        data[key] = None
                    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000),
                                      encoding="utf-8")
                    report["answers_nulled"].append({"job_id": job_id, "keys": bad,
                                                     "why": [p["detail"] for p in out["problems"]][:3]})

    if args.prune_empty:
        referenced = {d for st in index.values() for d in st.get("tailored_dirs") or []}
        for d in sorted(paths.APPLICATIONS.glob("*/*")):
            rel = str(d.relative_to(paths.APPLICATIONS))
            if d.is_dir() and not any(d.iterdir()) and rel not in referenced:
                report["pruned"].append(rel)
                if act:
                    d.rmdir()

    summary = {k: (len(v) if k != "answers_nulled" else v) for k, v in report.items()}
    print(json.dumps({"applied": act, **summary}, indent=2, ensure_ascii=False))
    if not act:
        print("dry run — re-run with --apply to write", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
