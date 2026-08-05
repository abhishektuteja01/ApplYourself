"""Deterministic state.yaml plumbing for /track, /tailor (tailored_dirs[]),
/cover-letter (cover_letters[]), /outreach (draft append), /standup
(read-only rollup).

Determinism boundary: no LLM calls. /track is the sole writer
of state transitions (R10); /tailor, /cover-letter and /outreach mutate
only the append-only side lists (tailored_dirs[], cover_letters[],
outreach[]); /standup is read-only.

State.yaml schema. State set + terminal-state rules.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml

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
    """Return the parsed state.yaml dict, or None if the file doesn't exist.
    Raises ValueError on malformed YAML or non-mapping content."""
    if not state_path.exists():
        return None
    try:
        data = yaml.safe_load(state_path.read_text()) or {}
    except (yaml.YAMLError, OSError) as e:
        raise ValueError(f"failed to read {state_path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{state_path} is not a YAML mapping")
    return data


def _write_state(state_path: Path, data: dict) -> None:
    """Atomic write: tmp file + rename, so a partial write can't corrupt
    the canonical state.yaml."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    tmp.replace(state_path)


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

    If state.yaml does NOT exist: creates it. Requires initial_fields to
    contain at minimum {job_id, company, title}; optional keys
    {source, url, location, vertical, sponsorship_label, fit_score}
    populate the template fields. `vertical` is set once at
    creation and never reclassified on later transitions.

    If state.yaml exists:
      - rejects new_state not in VALID_STATES
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
    existing = load_state(state_path)

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
            "cover_letters":     [],
            "notes":             "",
            "outreach":          [],
        }
    else:
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

    _write_state(state_path, data)
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
    existing = load_state(state_path)
    if existing is not None:
        return existing, False
    data = transition(
        state_path, "saved", note="auto-registered by /tailor",
        initial_fields=initial_fields, now=now,
    )
    return data, True


# ---------------------------------------------------------------
# Side-list mutations (/tailor + /cover-letter + /outreach helpers)
# ---------------------------------------------------------------

def append_tailored_dir(state_path: Path, dir_name: str,
                         *, now: datetime | None = None) -> dict:
    """Append dir_name to tailored_dirs[] (/tailor's mutation).
    Idempotent: appending the same dir twice keeps a single entry."""
    data = load_state(state_path)
    if data is None:
        raise ValueError(
            f"state.yaml at {state_path} does not exist; run /track "
            "<job_id> saved first to register the role."
        )
    dirs = list(data.get("tailored_dirs") or [])
    if dir_name not in dirs:
        dirs.append(dir_name)
    data["tailored_dirs"] = dirs
    data["last_touch"] = _now_iso(now)
    _write_state(state_path, data)
    return data


def append_cover_letter(state_path: Path, dir_name: str,
                         *, now: datetime | None = None) -> dict:
    """Append dir_name to cover_letters[] (/cover-letter's mutation,
    the same side-list pattern as append_tailored_dir).
    Idempotent: appending the same dir twice keeps a single entry."""
    data = load_state(state_path)
    if data is None:
        raise ValueError(
            f"state.yaml at {state_path} does not exist; run /track "
            "<job_id> saved first to register the role."
        )
    dirs = list(data.get("cover_letters") or [])
    if dir_name not in dirs:
        dirs.append(dir_name)
    data["cover_letters"] = dirs
    data["last_touch"] = _now_iso(now)
    _write_state(state_path, data)
    return data


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
    data = load_state(state_path)
    if data is None:
        raise ValueError(
            f"state.yaml at {state_path} does not exist; run /track "
            "<job_id> saved first to register the role."
        )
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
    entries = list(data.get("outreach") or [])
    entries.append(entry)
    data["outreach"] = entries
    data["last_touch"] = _now_iso(now)
    _write_state(state_path, data)
    return data


def mark_outreach_sent(
    state_path: Path,
    *,
    channel: str,
    to_name: str,
    now: datetime | None = None,
) -> dict:
    """Flip the LATEST matching outreach[] entry from draft -> sent.
    Raises ValueError if no draft entry matches (channel, to_name)."""
    data = load_state(state_path)
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
            _write_state(state_path, data)
            return data
    raise ValueError(
        f"no draft outreach found for channel={channel!r} to={to_name!r} "
        f"in {state_path}"
    )


# ---------------------------------------------------------------
# Read-only helpers (/standup, /score skip suppression)
# ---------------------------------------------------------------

def load_all_states(pipeline_dir: Path) -> list[dict]:
    """Glob pipeline/*/state.yaml; return parsed dicts in alphabetical
    order of job_id. Malformed files are skipped with a logged warning,
    not raised -- one bad file shouldn't break /standup."""
    if not pipeline_dir.exists():
        return []
    out: list[dict] = []
    for state_file in sorted(pipeline_dir.glob("*/state.yaml")):
        try:
            data = load_state(state_file)
        except ValueError as e:
            log.warning("Skipping %s: %s", state_file, e)
            continue
        if data:
            out.append(data)
    return out


def skip_count(state_data: dict) -> int:
    """Count of skip entries in state_history. Used by /score's skip
    suppression (rows with >=1 skip entries are suppressed from the
    shortlist)."""
    history = state_data.get("state_history") or []
    return sum(
        1 for h in history
        if isinstance(h, dict) and h.get("state") == "skip"
    )
