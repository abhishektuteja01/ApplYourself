"""`apply triage` — sort the roles that are ready for a form, without a model.

For each role it reads the form and the answer bank (`apply_cli.build`) and says
which pile it is in:

  ready    every field is answered from the bank or from facts, nothing guessed
  open     something is new, guessed, missing, or needs a cover letter or company
           answers: a session (/apply) has to decide it
  expired  the posting is gone
  manual   the board has no submit path
  error    the form could not be read

`--fill` then fills each ready role's form (no submit) the way `/apply` step 7
does, and writes the same run report, so the sheet moves the role to Submittable.
Nothing here judges anything (R7): every answer already exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from src import apply_cli, track_cli
from src.apply import sheet
from src.shared import clock, paths, schemas
from src.shared.files import atomic_write_text


def _open_detail(plan, guesses) -> dict:
    asks = list(plan.draftable) + [u for u in plan.unmapped if u.tier == "C" and u.required]
    facts = [u for u in plan.unmapped if (u.required or u.blocks) and u.tier != "C"]
    return {"guesses": len(guesses), "asks": len(asks), "facts": len(facts)}


def classify(job_id: str) -> dict:
    """{"job_id", "kind", "detail"}. Never raises."""
    try:
        plan, _ = apply_cli.build(job_id)
    except apply_cli.PostingExpired as exc:
        return {"job_id": job_id, "kind": "expired", "detail": str(exc)}
    except apply_cli.ManualApplyOnly as exc:
        return {"job_id": job_id, "kind": "manual", "detail": str(exc)}
    except apply_cli.BUILD_ERRORS as exc:
        return {"job_id": job_id, "kind": "error", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - one bad role must not stop the pass
        return {
            "job_id": job_id,
            "kind": "error",
            "detail": f"unexpected {type(exc).__name__}: {exc}",
        }
    guesses = [f for f in plan.fields if f.source.startswith("bank:") and "~" in f.source]
    if plan.submittable and not guesses and not plan.draftable and not plan.ignored_overrides:
        return {"job_id": job_id, "kind": "ready", "detail": ""}
    return {"job_id": job_id, "kind": "open", "detail": _open_detail(plan, guesses)}


def _drift(manifest: Path) -> int:
    """Filled fields the page holds something else for."""
    try:
        rows = json.loads(manifest.read_text(encoding="utf-8")).get("fields") or []
    except (OSError, ValueError):
        return 0
    return sum(1 for r in rows if r.get("group") == "filled" and not r.get("matches", True))


def _fill(job_id: str, headless: bool) -> dict:
    """Fill one ready role's form, no submit. The role becomes `tailored` first
    when only `saved`, as /apply's step 3 does when the board needs no letter."""
    rc = track_cli.main(
        [
            job_id,
            "tailored",
            "--if",
            "saved",
            "--note",
            "resume on file; no cover letter needed (apply triage)",
        ]
    )
    if rc:
        return {"job_id": job_id, "kind": "error", "detail": f"track exited {rc}"}
    out_dir = apply_cli.resolve_out_dir(job_id)
    manifest = out_dir / "form_manifest.json"
    outcome = apply_cli._run_role(job_id, submit=False, headless=headless, manifest_path=manifest)
    row = {"job_id": job_id, "kind": "ready", "detail": "", "_outcome": outcome}
    if outcome.category != "ready":
        return {**row, "kind": "error", "detail": f"{outcome.category}: {outcome.detail}"}
    drift = _drift(manifest)
    if drift:
        return {
            **row,
            "kind": "open",
            "detail": {
                "guesses": 0,
                "asks": 0,
                "facts": 0,
                "form": f"{drift} field(s) the page disagrees with",
            },
        }
    return row


def run(job_ids: list[str], *, fill: bool, headless: bool) -> dict:
    started = clock.now()
    result: dict = {
        "at": started.isoformat(timespec="seconds"),
        "ready": [],
        "open": {},
        "expired": [],
        "manual": [],
        "errors": {},
    }
    outcomes = []
    for job_id in job_ids:
        row = classify(job_id)
        if row["kind"] == "ready" and fill:
            row = _fill(job_id, headless)
            if row.get("_outcome") is not None:
                outcomes.append(row["_outcome"])
        kind = row["kind"]
        if kind == "ready":
            result["ready"].append(job_id)
        elif kind == "open":
            result["open"][job_id] = row["detail"]
        elif kind in ("expired", "manual"):
            result[kind].append(job_id)
        else:
            result["errors"][job_id] = row["detail"]
    if outcomes:
        path = apply_cli.reserve_report_path(started)
        apply_cli.write_report(outcomes, started, path=path)
        result["report"] = str(path)
    stamped = {"schema_version": schemas.APPLY_TRIAGE_SCHEMA_VERSION, **result}
    atomic_write_text(paths.APPLY_TRIAGE, json.dumps(stamped, indent=2), mkdir=True)
    return result


def render(result: dict) -> str:
    return (
        f"ready {len(result['ready'])} · open {len(result['open'])} · "
        f"expired {len(result['expired'])} · manual {len(result['manual'])} · "
        f"errors {len(result['errors'])}"
    )


def main(job_ids: list[str] | None, fill: bool, headless: bool, limit: int | None) -> int:
    ids = job_ids or sheet.job_ids("Resume ready")
    if limit is not None:
        ids = ids[:limit]
    result = run(ids, fill=fill, headless=headless)
    print(render(result))
    for job_id, msg in result["errors"].items():
        print(f"  {job_id}: {msg}")
    return 0
