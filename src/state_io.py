"""Deterministic state.yaml plumbing for /track, /tailor (tailored_dirs[]),
/outreach (draft append), /standup (read-only rollup).

Determinism boundary: no LLM calls. /track is the sole writer
of state transitions (R10); /tailor and /outreach mutate only the
append-only side lists (tailored_dirs[], outreach[]); /standup is read-only.
Whether a role has a cover letter or company answers is whatever files its
tailored_dirs[-1] folder holds.

State.yaml schema. State set + terminal-state rules.
"""
from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import yaml

from src.shared.parquet_io import locked

log = logging.getLogger(__name__)


# 11 states
VALID_STATES: frozenset[str] = frozenset({
    "saved", "skip", "tailored", "applied", "recruiter_contact",
    "screen", "interview", "offer", "rejected", "withdrawn", "ghosted",
})

# Terminal states reject all out-transitions
TERMINAL_STATES: frozenset[str] = frozenset({
    "offer", "rejected", "withdrawn", "ghosted",
})

# /standup table splits
ACTIVE_STATES: frozenset[str] = frozenset({
    "saved", "tailored", "applied", "recruiter_contact", "screen", "interview",
})
# Closed = terminal + skip (skip is NOT terminal but closed for /standup view)
CLOSED_STATES: frozenset[str] = TERMINAL_STATES | {"skip"}


# ---------------------------------------------------------------
# I/O primitives
# ---------------------------------------------------------------

def state_path_for(pipeline_dir: Path, job_id: str) -> Path:
    return pipeline_dir / job_id / "state.yaml"


def load_state(state_path: Path) -> dict | None:
    """Return the parsed state.yaml dict, or None if the file doesn't exist
    or is empty. An empty file carries no state, so callers must treat it the
    same as an absent one -- otherwise transition() takes its existing-role
    branch and writes a record with no job_id.
    Raises ValueError on malformed YAML or non-mapping content."""
    if not state_path.exists():
        return None
    try:
        data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        raise ValueError(f"failed to read {state_path}: {e}") from e
    if data is None or data == {}:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"{state_path} is not a YAML mapping")
    return data


def _write_state(state_path: Path, data: dict) -> None:
    """Atomic write: a temp file unique to this writer, fsynced, then renamed
    over state.yaml, so a partial write can't corrupt it."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_path.parent, prefix=f".{state_path.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(state_path: Path, fn: Callable[[dict | None], dict | None]) -> dict | None:
    """The one read-modify-write of a state.yaml: under the file's lock, pass
    the current dict (None when absent or empty) to fn and write what it
    returns. fn returning None writes nothing and returns the current dict."""
    with locked(state_path):
        current = load_state(state_path)
        data = fn(current)
        if data is None:
            return current
        _write_state(state_path, data)
        return data


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now()).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------
# State transitions (sole writer per R10)
# ---------------------------------------------------------------

def transition(
    state_path: Path,
    new_state: str,
    *,
    note: str | None = None,
    now: datetime | None = None,
    initial_fields: dict | None = None,
) -> dict:
    """Transition the role at state_path to new_state.

    If state.yaml does NOT exist (or is empty): creates it. Requires initial_fields to
    contain at minimum {job_id, company, title}; optional keys
    {source, url, location, vertical, sponsorship_label, fit_score}
    populate the template fields. `vertical` is set once at
    creation and never reclassified on later transitions.

    If state.yaml exists:
      - rejects new_state not in VALID_STATES
      - rejects a record with no job_id (corrupt file)
      - rejects any transition out of a TERMINAL_STATE
      - appends to state_history (append-only)
      - updates last_touch
      - sets applied_at on first transition to "applied"

    Raises ValueError on any of those rejections."""
    if new_state not in VALID_STATES:
        raise ValueError(
            f"unknown state {new_state!r}; valid: {sorted(VALID_STATES)}"
        )
    now_iso = _now_iso(now)
    return update(state_path, lambda existing: _transitioned(
        state_path, existing, new_state, note, now_iso, initial_fields))


def _transitioned(state_path: Path, existing: dict | None, new_state: str,
                  note: str | None, now_iso: str, initial_fields: dict | None) -> dict:
    if existing is None:
        if not initial_fields:
            raise ValueError(
                f"state.yaml at {state_path} does not exist; pass "
                "initial_fields={job_id, company, title, ...} to create it."
            )
        required = {"job_id", "company", "title"}
        missing = required - set(initial_fields)
        if missing:
            raise ValueError(
                f"initial_fields missing required keys: {sorted(missing)}"
            )
        data = {
            "job_id":            initial_fields["job_id"],
            "company":           initial_fields["company"],
            "title":             initial_fields["title"],
            "source":            initial_fields.get("source", ""),
            "url":               initial_fields.get("url", ""),
            "location":          initial_fields.get("location", ""),
            "vertical":          initial_fields.get("vertical", ""),
            "sponsorship_label": initial_fields.get("sponsorship_label", "unknown"),
            "fit_score":         initial_fields.get("fit_score"),
            "state":             new_state,
            "state_history":     [{"state": new_state, "at": now_iso,
                                   "note": note or ""}],
            "applied_at":        now_iso if new_state == "applied" else None,
            "last_touch":        now_iso,
            "tailored_dirs":     [],
            "notes":             "",
            "outreach":          [],
        }
    else:
        if not existing.get("job_id"):
            raise ValueError(
                f"{state_path} has no job_id -- the file is corrupt. Delete it "
                "and re-run /track to rebuild it from clean.parquet."
            )
        current = existing.get("state")
        if current in TERMINAL_STATES:
            raise ValueError(
                f"cannot transition out of terminal state {current!r} "
                f"(role at {state_path}); terminals: {sorted(TERMINAL_STATES)}"
            )
        data = dict(existing)
        history = list(data.get("state_history") or [])
        history.append({"state": new_state, "at": now_iso, "note": note or ""})
        data["state"] = new_state
        data["state_history"] = history
        data["last_touch"] = now_iso
        if new_state == "applied" and not data.get("applied_at"):
            data["applied_at"] = now_iso
    return data


def ensure_state(
    state_path: Path,
    initial_fields: dict,
    now: datetime | None = None,
) -> tuple[dict, bool]:
    """Bootstrap-only counterpart to transition(): create state.yaml at
    `saved` if absent, do nothing if present. Never a transition, so /tailor
    can register a role without writing state (R10) and a re-tailor of any
    role -- terminal included -- leaves `state` untouched.

    Returns (state_dict, created)."""
    created: list[bool] = []

    def create(existing: dict | None) -> dict | None:
        if existing is not None:
            return None
        created.append(True)
        return _transitioned(state_path, None, "saved", "auto-registered by /tailor",
                             _now_iso(now), initial_fields)

    return update(state_path, create), bool(created)


# ---------------------------------------------------------------
# Side-list mutations (/tailor + /outreach helpers)
# ---------------------------------------------------------------

def append_tailored_dir(state_path: Path, dir_name: str,
                         *, now: datetime | None = None) -> dict:
    """Append dir_name to tailored_dirs[] (/tailor's mutation).
    A dir already listed moves to the end, so [-1] is always the latest build."""
    def add(data: dict | None) -> dict:
        if data is None:
            raise ValueError(
                f"state.yaml at {state_path} does not exist; run /track "
                "<job_id> saved first to register the role."
            )
        dirs = [d for d in data.get("tailored_dirs") or [] if d != dir_name] + [dir_name]
        data["tailored_dirs"] = dirs
        data["last_touch"] = _now_iso(now)
        return data

    return update(state_path, add)


def append_outreach_draft(
    state_path: Path,
    *,
    channel: str,
    to_name: str,
    draft_file: str,
    contact_id: str | None = None,
    medium: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Append a new outreach[] entry with status=draft (/outreach
    mutation)."""
    entry = {
        "channel": channel,
        "to": to_name,
        "at": _now_iso(now),
        "status": "draft",
        "draft_file": draft_file,
    }
    if contact_id is not None:
        entry["contact_id"] = contact_id
    if medium is not None:
        entry["medium"] = medium

    def add(data: dict | None) -> dict:
        if data is None:
            raise ValueError(
                f"state.yaml at {state_path} does not exist; run /track "
                "<job_id> saved first to register the role."
            )
        data["outreach"] = [*(data.get("outreach") or []), entry]
        data["last_touch"] = _now_iso(now)
        return data

    return update(state_path, add)


def mark_outreach_sent(
    state_path: Path,
    *,
    channel: str,
    to_name: str,
    now: datetime | None = None,
) -> dict:
    """Flip the LATEST matching outreach[] entry from draft -> sent.
    Raises ValueError if no draft entry matches (channel, to_name)."""
    def flip(data: dict | None) -> dict:
        if data is None:
            raise ValueError(f"state.yaml at {state_path} does not exist")
        entries = list(data.get("outreach") or [])
        for i in range(len(entries) - 1, -1, -1):
            e = entries[i]
            if (isinstance(e, dict)
                    and e.get("channel") == channel
                    and e.get("to") == to_name
                    and e.get("status") == "draft"):
                entries[i] = {**e, "status": "sent"}
                data["outreach"] = entries
                data["last_touch"] = _now_iso(now)
                return data
        raise ValueError(
            f"no draft outreach found for channel={channel!r} to={to_name!r} "
            f"in {state_path}"
        )

    return update(state_path, flip)


# ---------------------------------------------------------------
# Read-only helpers (/standup, /score skip suppression)
# ---------------------------------------------------------------

def iter_states(pipeline_dir: Path):
    """Yield every readable pipeline/*/state.yaml dict, in alphabetical order.

    The one place the glob-and-skip-unreadable shape lives. Malformed files are
    skipped with a logged warning, not raised -- one bad file must not break
    /standup, a discovery run, or a shortlist.
    """
    if not pipeline_dir.exists():
        return
    for state_file in sorted(pipeline_dir.glob("*/state.yaml")):
        try:
            data = load_state(state_file)
        except ValueError as e:
            log.warning("Skipping %s: %s", state_file, e)
            continue
        if data:
            yield data


def load_all_states(pipeline_dir: Path) -> list[dict]:
    """Every state.yaml as a list, alphabetical. Sole reader for /standup."""
    return list(iter_states(pipeline_dir))


def load_state_index(pipeline_dir: Path) -> dict[str, dict]:
    """{job_id: state dict} for the consumers that key on job_id --
    the discovery view's application_status and the shortlist's skip suppression.
    Records without a str job_id are skipped: they cannot be joined to a row."""
    return {d["job_id"]: d for d in iter_states(pipeline_dir)
            if isinstance(d.get("job_id"), str)}
