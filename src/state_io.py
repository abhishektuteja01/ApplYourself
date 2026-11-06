"""Deterministic state.yaml plumbing for /track, /tailor (tailored_dirs[]),
/outreach (draft append), /standup (read-only rollup).

Determinism boundary: no LLM calls. /track is the sole writer
of state transitions (R10); /tailor and /outreach mutate only the
append-only side lists (tailored_dirs[], outreach[]); /standup is read-only.
Whether a role has a cover letter or company answers is whatever files its
tailored_dirs[-1] folder holds.

History: `pipeline/events.jsonl`, one append-only line per change. Each
state.yaml is a cache of the fold over its role's events, written under the
same lock, so readers keep reading state.yaml and `rebuild_states` can always
remake it. Until `track migrate-events --apply` creates the log, writes
update state.yaml alone, as before.

State.yaml schema. State set + terminal-state rules.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from src.shared import clock
from src.shared.parquet_io import locked
from src.shared.schemas import EVENT_SCHEMA_VERSION, STATE_SCHEMA_VERSION

log = logging.getLogger(__name__)


# 9 stored states; `ghosted` is derived on read (shown_state)
VALID_STATES: frozenset[str] = frozenset(
    {
        "saved",
        "skip",
        "tailored",
        "applied",
        "screen",
        "interview",
        "offer",
        "rejected",
        "withdrawn",
    }
)

# The moves /track makes without --force; any other move needs --force and a note.
ALLOWED: dict[str, frozenset[str]] = {
    "saved": frozenset({"tailored", "applied", "skip", "withdrawn"}),
    "tailored": frozenset({"applied", "saved", "skip", "withdrawn"}),
    "applied": frozenset({"screen", "interview", "offer", "rejected", "withdrawn"}),
    "screen": frozenset({"interview", "offer", "rejected", "withdrawn"}),
    "interview": frozenset({"offer", "rejected", "withdrawn"}),
    "skip": frozenset({"saved"}),
    "offer": frozenset(),
    "rejected": frozenset(),
    "withdrawn": frozenset(),
}

# Terminal states allow no move without --force
TERMINAL_STATES: frozenset[str] = frozenset(s for s, moves in ALLOWED.items() if not moves)

GHOSTED = "ghosted"

# /standup table splits
ACTIVE_STATES: frozenset[str] = frozenset(
    {
        "saved",
        "tailored",
        "applied",
        "screen",
        "interview",
    }
)
# Closed = terminal + skip + derived ghosted (skip is NOT terminal but closed for /standup view)
CLOSED_STATES: frozenset[str] = TERMINAL_STATES | {"skip", GHOSTED}


# ---------------------------------------------------------------
# I/O primitives
# ---------------------------------------------------------------


def state_path_for(pipeline_dir: Path, job_id: str) -> Path:
    return pipeline_dir / job_id / "state.yaml"


def load_state(state_path: Path) -> dict[str, Any] | None:
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
    # A missing version is 1: files written before versioning.
    version = data.get("schema_version", 1)
    if not isinstance(version, int) or version > STATE_SCHEMA_VERSION:
        raise ValueError(
            f"{state_path} has schema_version {version!r}; this code knows "
            f"up to {STATE_SCHEMA_VERSION}"
        )
    return data


def _write_state(state_path: Path, data: dict[str, Any]) -> None:
    """Atomic write: a temp file unique to this writer, fsynced, then renamed
    over state.yaml, so a partial write can't corrupt it."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_path.parent, prefix=f".{state_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


State = dict[str, Any]
Event = dict[str, Any]

EVENTS_NAME = "events.jsonl"

# Set at creation from the parquets; carried by the `created` event.
_FIELD_DEFAULTS: dict[str, Any] = {
    "company": "",
    "title": "",
    "source": "",
    "url": "",
    "location": "",
    "vertical": "",
}

# Old copies of the score; scored.parquet holds it. Kept only until `fields_dropped`.
SCORE_COPIES = ("sponsorship_label", "fit_score")


def update(
    state_path: Path, fn: Callable[[State | None], State | None], *, create: bool = False
) -> State | None:
    """A cache-only read-modify-write of a state.yaml under its lock, logging
    no event: only stamp_schema uses it. fn gets the current dict (None when
    absent or empty); returning None writes nothing and returns the current
    dict. Without create, an absent file is never locked, so no empty role
    folder is left behind."""
    if not create and not state_path.exists() and fn(None) is None:
        return None
    with locked(state_path):
        current = load_state(state_path)
        data = fn(current)
        if data is None:
            return current
        data = _stamped(data)
        _write_state(state_path, data)
        return data


def events_path_for(pipeline_dir: Path) -> Path:
    return pipeline_dir / EVENTS_NAME


def _log_of(state_path: Path) -> Path:
    return events_path_for(state_path.parent.parent)


def _event(
    job_id: str,
    kind: str,
    ts: str,
    *,
    from_state: str | None = None,
    to: str | None = None,
    note: str = "",
    forced: bool = False,
    data: dict[str, Any] | None = None,
) -> Event:
    return {
        "v": EVENT_SCHEMA_VERSION,
        "ts": ts,
        "job_id": job_id,
        "kind": kind,
        "from": from_state,
        "to": to,
        "note": note,
        "forced": forced,
        "data": data or {},
    }


def read_events(log_path: Path) -> list[Event]:
    """Every event, oldest first. A line that is not JSON is skipped with a
    warning; a newer schema than this code knows raises ValueError."""
    if not log_path.is_file():
        return []
    out = []
    for n, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            log.warning("%s:%d is not JSON; skipped", log_path, n)
            continue
        if not isinstance(ev, dict) or not isinstance(ev.get("v"), int):
            log.warning("%s:%d is not an event; skipped", log_path, n)
            continue
        if ev["v"] > EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"{log_path}:{n} has event version {ev['v']}; this code knows "
                f"up to {EVENT_SCHEMA_VERSION}"
            )
        out.append(ev)
    return out


def _append_event(log_path: Path, ev: Event) -> None:
    with locked(log_path), log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def write_events(log_path: Path, events: list[Event]) -> None:
    """Create the log in one atomic write (the migration). Refuses an
    existing log."""
    with locked(log_path):
        if log_path.exists():
            raise ValueError(f"{log_path} already exists")
        fd, tmp = tempfile.mkstemp(dir=log_path.parent, prefix=f".{log_path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, log_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def apply_event(data: State | None, ev: Event) -> State:
    """One fold step: the state after ev. The live writers and the fold both
    use it, so state.yaml and the log cannot disagree on what an event does."""
    kind, ts, payload = ev["kind"], ev["ts"], ev.get("data") or {}
    if kind == "created":
        to = ev["to"]
        return {
            "job_id": ev["job_id"],
            **{k: payload.get(k, default) for k, default in _FIELD_DEFAULTS.items()},
            **{k: payload[k] for k in SCORE_COPIES if k in payload},
            "state": to,
            "state_history": [{"state": to, "at": ts, "note": ev.get("note") or ""}],
            "applied_at": ts if to == "applied" else None,
            "last_touch": ts,
            "tailored_dirs": [],
            "notes": "",
            "outreach": [],
        }
    if data is None:
        raise ValueError(f"{ev['job_id']}: a {kind!r} event before the role was created")
    data = dict(data)
    if kind == "transition":
        entry: dict[str, Any] = {"state": ev["to"], "at": ts, "note": ev.get("note") or ""}
        if ev.get("forced"):
            entry["forced"] = True
        data["state_history"] = [*(data.get("state_history") or []), entry]
        data["state"] = ev["to"]
        data["last_touch"] = ts
        if ev["to"] == "applied" and not data.get("applied_at"):
            data["applied_at"] = ts
    elif kind == "tailored_dir":
        name = payload["dir"]
        data["tailored_dirs"] = [d for d in data.get("tailored_dirs") or [] if d != name] + [name]
        data["last_touch"] = ts
    elif kind == "outreach_drafted":
        data["outreach"] = [*(data.get("outreach") or []), payload["entry"]]
        data["last_touch"] = ts
    elif kind == "outreach_sent":
        entries = list(data.get("outreach") or [])
        i = _latest_draft(entries, payload["channel"], payload["to"])
        if i is not None:
            entries[i] = {**entries[i], "status": "sent"}
        data["outreach"] = entries
        data["last_touch"] = ts
    elif kind == "cleaned":
        data["cleaned"] = payload["cleaned"]
    elif kind == "imported":
        data.update(payload)
    elif kind == "fields_dropped":
        data = {k: v for k, v in data.items() if k not in payload["keys"]}
    else:
        raise ValueError(f"{ev['job_id']}: unknown event kind {kind!r}")
    return data


def fold(events: list[Event]) -> dict[str, State]:
    """{job_id: state dict} from the events, in log order. An event for a
    role with no `created` before it is skipped with a warning."""
    out: dict[str, State] = {}
    for ev in events:
        try:
            out[ev["job_id"]] = apply_event(out.get(ev["job_id"]), ev)
        except (KeyError, ValueError) as e:
            log.warning("event skipped: %s", e)
    return {j: _stamped(d) for j, d in out.items()}


def logged_state(state_path: Path) -> State | None:
    """The fold of this role's events, or None (no log, or no events)."""
    log_path = _log_of(state_path)
    if not log_path.is_file():
        return None
    job_id = state_path.parent.name
    return fold([e for e in read_events(log_path) if e.get("job_id") == job_id]).get(job_id)


def _commit(
    state_path: Path, build: Callable[[State | None], Event | None], *, create: bool = False
) -> State | None:
    """The one write of a role: under its lock, build(current) gives the
    event (None writes nothing); once the log exists the event is appended
    to it, then state.yaml is rewritten as apply_event(current, event). A
    missing state.yaml is first recovered from the log."""
    if (
        not create
        and not state_path.exists()
        and logged_state(state_path) is None
        and build(None) is None
    ):
        return None
    with locked(state_path):
        current = load_state(state_path)
        healed = False
        if current is None:
            current = logged_state(state_path)
            healed = current is not None
        ev = build(current)
        if ev is None:
            if healed and current is not None:
                _write_state(state_path, current)
            return current
        log_path = _log_of(state_path)
        if log_path.exists():
            _append_event(log_path, ev)
        data = _stamped(apply_event(current, ev))
        _write_state(state_path, data)
        return data


def _written(state_path: Path, data: State | None) -> State:
    if data is None:
        raise ValueError(f"state.yaml at {state_path} does not exist")
    return data


def _stamped(data: dict[str, Any]) -> dict[str, Any]:
    """data with schema_version set: first for a file that lacks it, in
    place otherwise."""
    if "schema_version" in data:
        return {**data, "schema_version": STATE_SCHEMA_VERSION}
    return {"schema_version": STATE_SCHEMA_VERSION, **data}


def needs_stamp(data: dict[str, Any] | None) -> bool:
    return data is not None and ("schema_version" not in data or "cover_letters" in data)


def stamp_schema(state_path: Path) -> bool:
    """Add schema_version to a state.yaml that lacks it and drop the dead
    cover_letters key. Never a transition. Returns True when it wrote."""
    wrote: list[bool] = []

    def stamp(data: dict[str, Any] | None) -> dict[str, Any] | None:
        if data is None or not needs_stamp(data):
            return None
        wrote.append(True)
        return {k: v for k, v in data.items() if k != "cover_letters"}

    update(state_path, stamp)
    return bool(wrote)


def _now_iso(now: datetime | None = None) -> str:
    return (now or clock.now()).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------
# State transitions (sole writer per R10)
# ---------------------------------------------------------------


class NotInState(ValueError):
    """transition(if_state=S) found the role in another state; nothing written."""

    def __init__(self, current: str | None) -> None:
        super().__init__(current)
        self.current = current


def transition(
    state_path: Path,
    new_state: str,
    *,
    note: str | None = None,
    now: datetime | None = None,
    initial_fields: dict[str, Any] | None = None,
    if_state: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Transition the role at state_path to new_state.

    if_state: move only when the role is in that state, checked under the
    lock; otherwise raise NotInState and write nothing.
    force: allow a move ALLOWED does not list; needs a note, and the history
    entry is marked forced.

    If state.yaml does NOT exist (or is empty): creates it. Requires initial_fields to
    contain at minimum {job_id, company, title}; optional keys
    {source, url, location, vertical} populate the template fields. `vertical` is set once at
    creation and never reclassified on later transitions.

    If state.yaml exists:
      - rejects new_state not in VALID_STATES
      - rejects a record with no job_id (corrupt file)
      - rejects a move ALLOWED does not list (same-state and out of a
        terminal included), unless force
      - appends to state_history (append-only)
      - updates last_touch
      - sets applied_at on first transition to "applied"

    Raises ValueError on any of those rejections."""
    if new_state not in VALID_STATES:
        raise ValueError(f"unknown state {new_state!r}; valid: {sorted(VALID_STATES)}")
    if force and not (note or "").strip():
        raise ValueError("a forced move needs a note saying why")
    now_iso = _now_iso(now)

    def step(existing: State | None) -> Event:
        if if_state is not None and (existing or {}).get("state") != if_state:
            raise NotInState((existing or {}).get("state"))
        return _transition_event(
            state_path, existing, new_state, note, now_iso, initial_fields, force=force
        )

    return _written(state_path, _commit(state_path, step, create=if_state is None))


def _transition_event(
    state_path: Path,
    existing: dict[str, Any] | None,
    new_state: str,
    note: str | None,
    now_iso: str,
    initial_fields: dict[str, Any] | None,
    *,
    force: bool = False,
) -> Event:
    if existing is None:
        if not initial_fields:
            raise ValueError(
                f"state.yaml at {state_path} does not exist; pass "
                "initial_fields={job_id, company, title, ...} to create it."
            )
        required = {"job_id", "company", "title"}
        missing = required - set(initial_fields)
        if missing:
            raise ValueError(f"initial_fields missing required keys: {sorted(missing)}")
        fields = {k: initial_fields.get(k, d) for k, d in _FIELD_DEFAULTS.items()}
        return _event(
            initial_fields["job_id"], "created", now_iso, to=new_state, note=note or "", data=fields
        )
    if not existing.get("job_id"):
        raise ValueError(
            f"{state_path} has no job_id -- the file is corrupt. Delete it "
            "and re-run /track to rebuild it from clean.parquet."
        )
    current = existing.get("state")
    allowed = ALLOWED.get(str(current), frozenset())
    if new_state not in allowed and not force:
        moves = ", ".join(sorted(allowed)) or "none (terminal)"
        raise ValueError(
            f"{current!r} -> {new_state!r} is not an allowed move (role at "
            f"{state_path}); from {current!r}: {moves}. To make it anyway: "
            '--force --note "<why>"'
        )
    return _event(
        existing["job_id"],
        "transition",
        now_iso,
        from_state=current,
        to=new_state,
        note=note or "",
        forced=new_state not in allowed,
    )


def _transitioned(
    state_path: Path,
    existing: dict[str, Any] | None,
    new_state: str,
    note: str | None,
    now_iso: str,
    initial_fields: dict[str, Any] | None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    return apply_event(
        existing,
        _transition_event(
            state_path, existing, new_state, note, now_iso, initial_fields, force=force
        ),
    )


def ensure_state(
    state_path: Path,
    initial_fields: dict[str, Any],
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Bootstrap-only counterpart to transition(): create state.yaml at
    `saved` if absent, do nothing if present. Never a transition, so /tailor
    can register a role without writing state (R10) and a re-tailor of any
    role -- terminal included -- leaves `state` untouched.

    Returns (state_dict, created)."""
    created: list[bool] = []

    def create(existing: dict[str, Any] | None) -> Event | None:
        if existing is not None:
            return None
        created.append(True)
        return _transition_event(
            state_path, None, "saved", "auto-registered by /tailor", _now_iso(now), initial_fields
        )

    return _written(state_path, _commit(state_path, create, create=True)), bool(created)


# ---------------------------------------------------------------
# Side-list mutations (/tailor + /outreach helpers)
# ---------------------------------------------------------------


def _missing(state_path: Path) -> ValueError:
    return ValueError(
        f"state.yaml at {state_path} does not exist; run /track "
        "<job_id> saved first to register the role."
    )


def append_tailored_dir(
    state_path: Path, dir_name: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Append dir_name to tailored_dirs[] (/tailor's mutation).
    A dir already listed moves to the end, so [-1] is always the latest build."""

    def add(data: dict[str, Any] | None) -> Event:
        if data is None:
            raise _missing(state_path)
        return _event(data["job_id"], "tailored_dir", _now_iso(now), data={"dir": dir_name})

    return _written(state_path, _commit(state_path, add))


def append_outreach_draft(
    state_path: Path,
    *,
    channel: str,
    to_name: str,
    draft_file: str,
    contact_id: str | None = None,
    medium: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append a new outreach[] entry with status=draft (/outreach
    mutation)."""
    at = _now_iso(now)
    entry = {
        "channel": channel,
        "to": to_name,
        "at": at,
        "status": "draft",
        "draft_file": draft_file,
    }
    if contact_id is not None:
        entry["contact_id"] = contact_id
    if medium is not None:
        entry["medium"] = medium

    def add(data: dict[str, Any] | None) -> Event:
        if data is None:
            raise _missing(state_path)
        return _event(data["job_id"], "outreach_drafted", at, data={"entry": entry})

    return _written(state_path, _commit(state_path, add))


def _latest_draft(entries: list[Any], channel: str, to_name: str) -> int | None:
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if (
            isinstance(e, dict)
            and e.get("channel") == channel
            and e.get("to") == to_name
            and e.get("status") == "draft"
        ):
            return i
    return None


def mark_outreach_sent(
    state_path: Path,
    *,
    channel: str,
    to_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Flip the LATEST matching outreach[] entry from draft -> sent.
    Raises ValueError if no draft entry matches (channel, to_name)."""

    def flip(data: dict[str, Any] | None) -> Event:
        if data is None:
            raise ValueError(f"state.yaml at {state_path} does not exist")
        if _latest_draft(list(data.get("outreach") or []), channel, to_name) is None:
            raise ValueError(
                f"no draft outreach found for channel={channel!r} to={to_name!r} in {state_path}"
            )
        return _event(
            data["job_id"],
            "outreach_sent",
            _now_iso(now),
            data={"channel": channel, "to": to_name},
        )

    return _written(state_path, _commit(state_path, flip))


def mark_cleaned(
    state_path: Path, cleaned: dict[str, Any], *, now: datetime | None = None
) -> State | None:
    """Record `cleaned: {date, archived}` (track cleanup); None for an
    untracked role. Never a transition."""

    def mark(data: dict[str, Any] | None) -> Event | None:
        if data is None:
            return None
        return _event(data["job_id"], "cleaned", _now_iso(now), data={"cleaned": cleaned})

    return _commit(state_path, mark)


def drop_score_copies(state_path: Path, *, now: datetime | None = None) -> State | None:
    """Remove the old SCORE_COPIES keys (track drop-score-copies); None for an
    untracked role. Never a transition."""

    def drop(data: dict[str, Any] | None) -> Event | None:
        keys = [k for k in SCORE_COPIES if data is not None and k in data]
        if data is None or not keys:
            return None
        return _event(data["job_id"], "fields_dropped", _now_iso(now), data={"keys": keys})

    return _commit(state_path, drop)


# ---------------------------------------------------------------
# The migration and the rebuild (track migrate-events / rebuild-states)
# ---------------------------------------------------------------


def as_cached(record: State) -> State:
    """What the fold gives for a migrated state.yaml: stamped, without the
    dead cover_letters key."""
    return _stamped({k: v for k, v in record.items() if k != "cover_letters"})


def events_from_record(record: State) -> list[Event]:
    """A state.yaml as events: `created` from its first history entry, one
    `transition` per later entry, then `imported` with its side fields."""
    job_id = str(record["job_id"])
    history = [e for e in record.get("state_history") or [] if isinstance(e, dict)]
    first = (
        history[0] if history else {"state": record.get("state"), "at": record.get("last_touch")}
    )
    fields = {k: record.get(k, d) for k, d in _FIELD_DEFAULTS.items()}
    fields.update({k: record[k] for k in SCORE_COPIES if k in record})
    events = [
        _event(
            job_id,
            "created",
            str(first.get("at") or ""),
            to=first.get("state"),
            note=first.get("note") or "",
            data=fields,
        )
    ]
    prev = first.get("state")
    for e in history[1:]:
        events.append(
            _event(
                job_id,
                "transition",
                str(e.get("at") or ""),
                from_state=prev,
                to=e.get("state"),
                note=e.get("note") or "",
                forced=bool(e.get("forced")),
            )
        )
        prev = e.get("state")
    skip = {
        "schema_version",
        "job_id",
        "state",
        "state_history",
        "cover_letters",
        *_FIELD_DEFAULTS,
        *SCORE_COPIES,
    }
    side = {k: v for k, v in record.items() if k not in skip}
    events.append(
        _event(
            job_id, "imported", str(record.get("last_touch") or first.get("at") or ""), data=side
        )
    )
    return events


def events_from_ledger(
    job_id: str, line: dict[str, Any], fields: dict[str, Any], *, rejected: bool
) -> list[Event]:
    """A role the submit ledger says was sent but that has no state.yaml:
    created at `applied`, then `rejected` when the board turned it down."""
    at = str(line.get("at") or "")
    events = [
        _event(
            job_id,
            "created",
            at,
            to="applied",
            note="restored from the submit ledger",
            data={k: fields.get(k, d) for k, d in _FIELD_DEFAULTS.items()},
        )
    ]
    if rejected:
        events.append(
            _event(
                job_id,
                "transition",
                at,
                from_state="applied",
                to="rejected",
                note="the board rejected it, per the submit ledger",
            )
        )
    return events


def rebuild_states(pipeline_dir: Path, *, apply: bool) -> list[str]:
    """The job_ids whose state.yaml differs from the fold of the log
    (missing included); with apply, rewrite each from the fold."""
    differ = []
    for job_id, folded in sorted(fold(read_events(events_path_for(pipeline_dir))).items()):
        p = state_path_for(pipeline_dir, job_id)
        try:
            cached = load_state(p)
        except ValueError:
            cached = None
        if cached == folded:
            continue
        differ.append(job_id)
        if apply:
            with locked(p):
                _write_state(p, folded)
    return differ


# ---------------------------------------------------------------
# Read-only helpers (/standup, /score skip suppression)
# ---------------------------------------------------------------


def ghosted_after_days(data: dict[str, Any] | None = None) -> int:
    from src.shared.config_schema import setting

    return int(setting("ghosted_after_days", 30, data))


def shown_state(
    data: dict[str, Any], *, days: int | None = None, now: datetime | None = None
) -> str:
    """The state to show: `ghosted` for an `applied` role whose last
    state_history entry is older than ghosted_after_days, else the stored state."""
    current = str(data.get("state") or "")
    if current != "applied":
        return current
    history = data.get("state_history") or []
    last = history[-1].get("at") if history and isinstance(history[-1], dict) else None
    try:
        at = datetime.fromisoformat(str(last))
    except ValueError:
        return current
    limit = ghosted_after_days() if days is None else days
    age = (now or clock.now()) - at.replace(tzinfo=None)
    return GHOSTED if age > timedelta(days=limit) else current


def iter_states(pipeline_dir: Path) -> Iterator[dict[str, Any]]:
    """Yield every readable pipeline/*/state.yaml dict, in alphabetical order.

    The one place the glob-and-skip-unreadable shape lives. Malformed files are
    skipped with a logged warning, not raised -- one bad file must not break
    /standup, a discovery run, or a shortlist. A role in the log with no
    state.yaml gets its cache written from the fold first.
    """
    if not pipeline_dir.exists():
        return
    files = {p.parent.name: p for p in pipeline_dir.glob("*/state.yaml")}
    try:
        folded = fold(read_events(events_path_for(pipeline_dir)))
    except ValueError as e:
        log.warning("Skipping the event log: %s", e)
        folded = {}
    for job_id in sorted(files.keys() | folded.keys()):
        state_file = files.get(job_id) or state_path_for(pipeline_dir, job_id)
        if job_id not in files:
            with locked(state_file):
                if not state_file.exists():
                    _write_state(state_file, folded[job_id])
        try:
            data = load_state(state_file)
        except ValueError as e:
            log.warning("Skipping %s: %s", state_file, e)
            continue
        if data:
            yield data


def load_all_states(pipeline_dir: Path) -> list[dict[str, Any]]:
    """Every state.yaml as a list, alphabetical. Sole reader for /standup."""
    return list(iter_states(pipeline_dir))


def load_state_index(pipeline_dir: Path) -> dict[str, dict[str, Any]]:
    """{job_id: state dict} for the consumers that key on job_id --
    the discovery view's application_status and the shortlist's skip suppression.
    Records without a str job_id are skipped: they cannot be joined to a row."""
    return {d["job_id"]: d for d in iter_states(pipeline_dir) if isinstance(d.get("job_id"), str)}
