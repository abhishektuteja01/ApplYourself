"""Resolve a rendered application field to the value that goes into it.

Deterministic: config lookup, structural rules for the blocks Greenhouse owns,
and keyword rules for the employer-authored ones. Nothing here judges, drafts or
guesses — a field this module cannot resolve comes back parked, and a parked
field stops the submission.

Resolution order, keyed on the merged field's section and id before its prose:

    A   identity / education / employment  -> application_answers.yaml blocks
    A2  eeoc / demographic                 -> opt out, structurally
    B0  work authorization                 -> work_authorization.status
    B   question_<digits>                  -> rules[]
    C   anything left                      -> park if required, skip if not

Work authorization is its own block rather than a keyword rule because one rule
cannot answer both halves of it. Five of the eleven captured boards ask "are you
legally authorized to work in the U.S." and "will you now or in the future
require sponsorship" side by side, and for a citizen those are Yes and No. A
`visa` keyword also hits "What visa type do you hold?" and an `authorized to
work` one hits "Are you legally authourized to work in South Africa?" — both
free of any answer this module could supply.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

from src import paths
from src.apply.reconcile import MergedField

PROFILE = paths.PROFILE
DEFAULT_PATH = PROFILE / "application_answers.yaml"
EXAMPLE_PATH = PROFILE / "application_answers.example.yaml"
PREFERENCES_PATH = PROFILE / "preferences.md"

_SCHEMA_VERSION = 1

IDENTITY_KEYS = (
    "first_name",
    "last_name",
    "preferred_name",
    "email",
    "phone",
    "location",
    "country",
)
EDUCATION_KEYS = ("school", "degree", "discipline", "start_year", "end_year")
EDUCATION_OPTIONAL_KEYS = ("start_month", "end_month")
EMPLOYMENT_KEYS = (
    "company_name",
    "title",
    "start_month",
    "start_year",
    "end_month",
    "end_year",
)

# status -> (authorized to work in the US now, will require sponsorship at some
# point). The two answers are derived, never configured separately: a pair the
# user can set independently is a pair that can contradict itself.
WORK_AUTHORIZATION_STATUSES = {
    "citizen_or_pr": (True, False),
    "needs_sponsorship_now": (False, True),
    "time_limited": (True, True),
}

# The DOM ids the identity block fills, mapped to their config key. `location`
# renders as `candidate-location`; reconcile keeps the DOM id.
_IDENTITY_IDS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "preferred_name": "preferred_name",
    "email": "email",
    "phone": "phone",
    "candidate-location": "location",
    "country": "country",
}

# Handled by plan.py, which knows the role's /tailor output dir.
FILE_IDS = frozenset({"resume", "cover_letter"})

# id base -> config key, for the two repeating blocks. Education suffixes with
# `--N`, employment with `-N`; only entry 0 is filled.
_EDUCATION_IDS = {
    "school": "school",
    "degree": "degree",
    "discipline": "discipline",
    "start-year": "start_year",
    "end-year": "end_year",
    "start-month": "start_month",
    "end-month": "end_month",
}
_EMPLOYMENT_IDS = {
    "company-name": "company_name",
    "title": "title",
    "start-date-month": "start_month",
    "start-date-year": "start_year",
    "end-date-month": "end_month",
    "end-date-year": "end_year",
}
_EDUCATION_SUFFIX = re.compile(r"^(?P<base>[a-z-]+)--(?P<n>\d+)$")
# The employment checkbox carries an option suffix its name does not:
# id="current-role-0_1", name="current-role-0".
_EMPLOYMENT_SUFFIX = re.compile(r"^(?P<base>[a-z-]+)-(?P<n>\d+)(?:_\d+)?$")

# EEOC opt-out, per DOM id, matched against the options the form actually
# renders. Every board observed offers exactly these strings.
_EEOC_OPT_OUT = {
    "gender": ("Decline To Self Identify",),
    "race": ("Decline To Self Identify",),
    "veteran_status": ("I don't wish to answer",),
    "disability_status": ("I do not want to answer",),
}
# hispanic_ethnicity is DOM-only: no API question, so no option list to match a
# typed string against, and optional on every board seen. Left untouched.
_EEOC_LEAVE_BLANK = frozenset({"hispanic_ethnicity"})

# Tried in order after the per-id string above, and for demographic questions
# whose decline option carries no flag.
_OPT_OUT_FALLBACKS = (
    "I don't wish to answer",
    "I do not wish to answer",
    "I do not want to answer",
    "Decline To Self Identify",
    "Decline to self identify",
    "I prefer not to answer",
    "Prefer not to say",
)

# Anything work-authorization shaped. A rules[] keyword that lands in here is a
# load error; a question label that lands in here is answered by exactly one of
# the two families below, or parked.
WORK_AUTHORIZATION_DOMAIN = re.compile(
    r"sponsor|visa|work\s+authoriz|authou?riz\w*\s+to\s+work|citizenship|right\s+to\s+work",
    re.IGNORECASE,
)
_AUTHORIZED_FAMILY = re.compile(
    r"authou?riz\w*\s+to\s+work|legally\s+authou?riz", re.IGNORECASE
)
_SPONSORSHIP_FAMILY = re.compile(
    r"(?:requir|need)\w*[^?]*sponsor|sponsor\w*[^?]*(?:requir|need)", re.IGNORECASE
)
# "Are you able to work without sponsorship?" inverts the answer. Never guess it.
_SPONSORSHIP_NEGATED = re.compile(
    r"without\s+(?:\w+\s+){0,2}sponsor|not\s+(?:\w+\s+){0,2}requir\w*[^?]*sponsor",
    re.IGNORECASE,
)
# The authorization question is country-scoped; the sponsorship one is not, and
# names a country on only 2 of the 5 captured boards. The queue only ever holds
# roles that passed discovery's US location allowlist.
_NAMES_THE_US = re.compile(
    r"united\s+states|u\.s\.a?\.|\bu\.s\b|\busa\b|america", re.IGNORECASE
)
_NAMES_US_ABBREV = re.compile(r"\bUS\b")  # case-sensitive: "us" is a pronoun

_YES_NO = {True: "Yes", False: "No"}

# preferences.md is prose, so the check is non-contradiction rather than a
# parse: derive whatever statuses the Work authorization section states, and
# refuse to load unless exactly one is derivable and it is the declared one.
_PREFERENCES_MARKERS = {
    "citizen_or_pr": re.compile(
        r"permanent\s+resident|green\s*card|\bu\.?s\.?\s+citizen\b", re.IGNORECASE
    ),
    "needs_sponsorship_now": re.compile(
        r"needs?\s+sponsorship\s+now|sponsorship\s+from\s+day\s+one"
        r"|requires?\s+(?:visa\s+)?sponsorship\s+(?:now|from\s+day\s+one)",
        re.IGNORECASE,
    ),
    "time_limited": re.compile(
        r"\bF-1\b|\bOPT\b|STEM\s+extension|time-limited\s+work\s+authorization"
    ),
}
_PREFERENCES_SECTION = re.compile(
    r"^##\s+work\s+authorization\s*$(?P<body>.*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

_PUNCT = re.compile(r"[^\w\s]+")
_WS = re.compile(r"\s+")


class AnswersError(Exception):
    """The answer config cannot be loaded, or contradicts preferences.md."""


def _norm(text: str) -> str:
    """Casefolded, punctuation-free, single-spaced — the form both rule keywords
    and question labels are compared in."""
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'")
    return _WS.sub(" ", _PUNCT.sub(" ", text.casefold())).strip()


def _norm_option(text: str) -> str:
    """Same, but apostrophes survive: the opt-out strings differ only in prose,
    and stripping punctuation would merge nothing useful."""
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'")
    return _WS.sub(" ", text.casefold()).strip()


@dataclass(frozen=True)
class Rule:
    match: tuple[str, ...]      # normalized keywords
    answers: tuple[str, ...]    # candidates, in preference order


@dataclass(frozen=True)
class Answers:
    identity: dict[str, str]
    education: dict[str, str]
    employment: dict[str, str] | None
    status: str
    rules: tuple[Rule, ...]

    @property
    def authorized_now(self) -> bool:
        return WORK_AUTHORIZATION_STATUSES[self.status][0]

    @property
    def requires_sponsorship(self) -> bool:
        return WORK_AUTHORIZATION_STATUSES[self.status][1]


@dataclass(frozen=True)
class Resolution:
    """What to do with one field. `fill` carries a value; `skip` leaves an
    optional field alone; `park` stops the whole submission; `defer` hands the
    field to plan.py, which owns the /tailor output dir."""

    action: str                                 # fill | skip | park | defer
    value: str | tuple[str, ...] | bool | None = None
    tier: str = ""
    reason: str = ""

    @property
    def parked(self) -> bool:
        return self.action == "park"


def _fill(value, tier: str) -> Resolution:
    return Resolution("fill", value=value, tier=tier)


def _skip(reason: str, tier: str = "") -> Resolution:
    return Resolution("skip", tier=tier, reason=reason)


def _park(reason: str, tier: str = "") -> Resolution:
    return Resolution("park", tier=tier, reason=reason)


# ---------------------------------------------------------------- loading


def _require_str(value, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AnswersError(f"{where}: expected a string, got {value!r}")
    text = str(value).strip()
    if not text:
        raise AnswersError(f"{where}: is empty")
    return text


def _parse_block(data, key: str, required: tuple[str, ...],
                 optional: tuple[str, ...] = ()) -> dict[str, str]:
    block = data.get(key)
    if not isinstance(block, dict):
        raise AnswersError(f"{key}: missing, or not a mapping")
    out = {}
    for field_key in required:
        out[field_key] = _require_str(block.get(field_key), f"{key}.{field_key}")
    for field_key in optional:
        if block.get(field_key) is not None:
            out[field_key] = _require_str(block[field_key], f"{key}.{field_key}")
    unknown = set(block) - set(required) - set(optional) - {"current_role"}
    if unknown:
        raise AnswersError(f"{key}: unknown keys {sorted(unknown)}")
    return out


def _parse_rules(data) -> tuple[Rule, ...]:
    raw = data.get("rules")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise AnswersError("rules: not a list")

    rules = []
    for i, entry in enumerate(raw):
        where = f"rules[{i}]"
        if not isinstance(entry, dict):
            raise AnswersError(f"{where}: not a mapping")
        match = entry.get("match")
        if not isinstance(match, list) or not match:
            raise AnswersError(f"{where}.match: must be a non-empty list")
        keywords = tuple(_norm(_require_str(k, f"{where}.match")) for k in match)
        if any(not k for k in keywords):
            raise AnswersError(f"{where}.match: a keyword normalizes to nothing")
        for keyword in keywords:
            if WORK_AUTHORIZATION_DOMAIN.search(keyword):
                raise AnswersError(
                    f"{where}.match: {keyword!r} is a work-authorization keyword. "
                    "Those questions are answered from work_authorization.status, "
                    "not from rules — one rule cannot answer both 'are you "
                    "authorized to work' and 'will you require sponsorship'."
                )
        answer = entry.get("answer")
        candidates = answer if isinstance(answer, list) else [answer]
        if not candidates:
            raise AnswersError(f"{where}.answer: must not be empty")
        answers = tuple(_require_str(a, f"{where}.answer") for a in candidates)
        unknown = set(entry) - {"match", "answer"}
        if unknown:
            raise AnswersError(f"{where}: unknown keys {sorted(unknown)}")
        rules.append(Rule(match=keywords, answers=answers))

    # Matching is substring-and-first-wins, so an overlap is not a tie the file
    # order resolves — it is a rule silently shadowing another, which is how a
    # salary rule ends up answering a start-date question.
    for i, rule in enumerate(rules):
        for j, other in enumerate(rules):
            if j <= i:
                continue
            for a in rule.match:
                for b in other.match:
                    if a in b or b in a:
                        raise AnswersError(
                            f"rules[{i}].match {a!r} overlaps rules[{j}].match {b!r}: "
                            "one rule would shadow the other"
                        )
    return tuple(rules)


def preferences_statuses(text: str) -> set[str]:
    """Every work-authorization status the Work authorization section states."""
    section = _PREFERENCES_SECTION.search(text or "")
    body = section.group("body") if section else ""
    return {name for name, pattern in _PREFERENCES_MARKERS.items() if pattern.search(body)}


def _check_preferences(status: str, preferences_path: Path) -> None:
    """An auto-submitted work-authorization answer is a legal claim sent under
    the user's name, so a check that cannot read preferences.md fails rather
    than passing quietly."""
    if not preferences_path.exists():
        raise AnswersError(
            f"{preferences_path} missing: work_authorization.status cannot be "
            f"cross-checked. Copy {PROFILE / 'preferences.example.md'} and fill it in."
        )
    found = preferences_statuses(preferences_path.read_text(encoding="utf-8"))
    if not found:
        raise AnswersError(
            f"{preferences_path}: the '## Work authorization' section states no "
            "recognizable status. Keep exactly one of the three bullets from "
            "preferences.example.md so the check has something to compare against."
        )
    if len(found) > 1:
        raise AnswersError(
            f"{preferences_path}: the '## Work authorization' section states "
            f"{sorted(found)} at once. Keep exactly one."
        )
    stated = found.pop()
    if stated != status:
        raise AnswersError(
            f"work_authorization.status is {status!r} but {preferences_path} "
            f"states {stated!r}. Fix whichever is wrong before applying."
        )


def load_answers(path: Path | None = None, preferences_path: Path | None = None) -> Answers:
    """Load and validate the answer config. Fails loud on anything ambiguous."""
    p = Path(path) if path is not None else DEFAULT_PATH
    if not p.exists():
        raise AnswersError(f"{p} missing. Copy {EXAMPLE_PATH} and fill it in.")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AnswersError(f"Malformed YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnswersError(f"{p}: top level must be a mapping")
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise AnswersError(f"{p}: schema_version must be {_SCHEMA_VERSION}")

    identity = _parse_block(data, "identity", IDENTITY_KEYS)
    education = _parse_block(data, "education", EDUCATION_KEYS, EDUCATION_OPTIONAL_KEYS)

    employment = None
    if data.get("employment") is not None:
        employment = _parse_block(data, "employment", EMPLOYMENT_KEYS)
        employment["current_role"] = bool(data["employment"].get("current_role"))

    work_auth = data.get("work_authorization")
    if not isinstance(work_auth, dict):
        raise AnswersError("work_authorization: missing, or not a mapping")
    status = _require_str(work_auth.get("status"), "work_authorization.status")
    if status not in WORK_AUTHORIZATION_STATUSES:
        raise AnswersError(
            f"work_authorization.status: {status!r} is not one of "
            f"{sorted(WORK_AUTHORIZATION_STATUSES)}"
        )
    _check_preferences(
        status, Path(preferences_path) if preferences_path is not None else PREFERENCES_PATH
    )

    return Answers(
        identity=identity,
        education=education,
        employment=employment,
        status=status,
        rules=_parse_rules(data),
    )


# ---------------------------------------------------------------- resolving


def _pick_option(field: MergedField, candidates: tuple[str, ...]) -> str | None:
    """The first candidate the widget actually offers.

    With no option list — every DOM-only react-select, `country` included —
    there is nothing to check against, so the first candidate goes through and
    fill.py's post-selection assert is what catches a string the widget rejects.
    """
    if not field.options:
        return candidates[0] if candidates else None
    offered = {_norm_option(o.label): o.label for o in field.options}
    for candidate in candidates:
        hit = offered.get(_norm_option(candidate))
        if hit is not None:
            return hit
    return None


def _resolve_choice(field: MergedField, candidates: tuple[str, ...], tier: str,
                    what: str) -> Resolution:
    picked = _pick_option(field, candidates)
    if picked is None:
        offered = [o.label for o in field.options]
        return _park(f"{what}: none of {list(candidates)} is offered ({offered})", tier)
    return _fill((picked,) if field.multi else picked, tier)


def _resolve_identity(field: MergedField, answers: Answers) -> Resolution | None:
    if field.id in FILE_IDS:
        return Resolution("defer", tier="A", reason=field.id)
    key = _IDENTITY_IDS.get(field.id)
    if key is None:
        return None
    value = answers.identity[key]
    if field.kind == "react_select":
        return _resolve_choice(field, (value,), "A", f"identity.{key}")
    return _fill(value, "A")


def _resolve_repeating(field: MergedField, block: dict[str, str] | None, ids: dict[str, str],
                       suffix: re.Pattern, config_key: str) -> Resolution:
    match = suffix.match(field.id)
    if match is None:
        return _park(f"{config_key}: unrecognized field id {field.id!r}", "A")
    if match.group("n") != "0":
        # First cut fills one entry and never clicks add-another; a board that
        # pre-renders a second one is a shape nothing here has seen.
        return (_park(f"{config_key}: a second entry is required", "A")
                if field.required else _skip("only entry 0 is filled", "A"))
    if block is None:
        missing = (f"{config_key}: this board renders an {config_key} block and "
                   f"profile/application_answers.yaml has no {config_key}: section")
        return _park(missing, "A") if field.required else _skip(missing, "A")
    base = match.group("base")
    if base == "current-role":
        return _fill(bool(block.get("current_role")), "A")
    key = ids.get(base)
    if key is None:
        return _park(f"{config_key}: unrecognized field id {field.id!r}", "A")
    value = block.get(key)
    if value is None:
        return (_park(f"{config_key}.{key}: required by this board, not set", "A")
                if field.required else _skip(f"{config_key}.{key} not set", "A"))
    if field.kind == "react_select":
        return _resolve_choice(field, (str(value),), "A", f"{config_key}.{key}")
    return _fill(str(value), "A")


def _resolve_eeoc(field: MergedField) -> Resolution:
    if field.id in _EEOC_LEAVE_BLANK:
        return (_park(f"{field.id}: required, and no option list to opt out against", "A2")
                if field.required else _skip("optional, left blank by policy", "A2"))
    preferred = _EEOC_OPT_OUT.get(field.id, ())
    candidates = preferred + tuple(o for o in _OPT_OUT_FALLBACKS if o not in preferred)
    picked = _pick_option(field, candidates) if field.options else None
    if picked is None:
        return (_park(f"{field.id}: no opt-out option offered", "A2")
                if field.required else _skip("no opt-out option offered", "A2"))
    return _fill((picked,) if field.multi else picked, "A2")


def _resolve_demographic(field: MergedField) -> Resolution:
    """The labels are employer-authored and vary, so the label is ignored: take
    the flagged decline option, else an exact opt-out string, else nothing."""
    flagged = tuple(o.label for o in field.options if o.decline_to_answer)
    picked = _pick_option(field, flagged) if flagged else None
    if picked is None:
        picked = _pick_option(field, _OPT_OUT_FALLBACKS) if field.options else None
    if picked is None:
        return (_park("demographic: no decline option offered", "A2")
                if field.required else _skip("no decline option offered", "A2"))
    return _fill((picked,) if field.multi else picked, "A2")


def _resolve_work_authorization(field: MergedField, answers: Answers) -> Resolution:
    label = field.label or ""
    authorized = bool(_AUTHORIZED_FAMILY.search(label))
    sponsorship = bool(_SPONSORSHIP_FAMILY.search(label)) and not _SPONSORSHIP_NEGATED.search(label)

    if authorized and sponsorship:
        return _park("work authorization: label reads as both families", "B0")
    if authorized:
        if not (_NAMES_THE_US.search(label) or _NAMES_US_ABBREV.search(label)):
            return _park("work authorization: the question names no country", "B0")
        value = answers.authorized_now
    elif sponsorship:
        value = answers.requires_sponsorship
    else:
        return _park("work authorization: label matches no answerable family", "B0")

    if not field.options:
        return _park("work authorization: not a select, so Yes/No does not fit", "B0")
    return _resolve_choice(field, (_YES_NO[value],), "B0", "work authorization")


def _resolve_rule(field: MergedField, answers: Answers) -> Resolution | None:
    label = _norm(field.label)
    if not label:
        return None
    for rule in answers.rules:
        if any(keyword in label for keyword in rule.match):
            if field.kind == "file":
                return _park("a rule cannot answer a file upload", "B")
            if field.options or field.kind == "react_select":
                return _resolve_choice(field, rule.answers, "B", "rule")
            return _fill(rule.answers[0], "B")
    return None


def resolve(field: MergedField, answers: Answers) -> Resolution:
    """Resolve one reconciled field. Never raises on content — an unanswerable
    field comes back parked (required) or skipped (optional)."""
    if field.section == "eeoc":
        return _resolve_eeoc(field)
    if field.section == "demographic":
        return _resolve_demographic(field)
    if field.section == "education":
        return _resolve_repeating(
            field, answers.education, _EDUCATION_IDS, _EDUCATION_SUFFIX, "education"
        )
    if field.section == "employment":
        return _resolve_repeating(
            field, answers.employment, _EMPLOYMENT_IDS, _EMPLOYMENT_SUFFIX, "employment"
        )

    identity = _resolve_identity(field, answers)
    if identity is not None:
        return identity

    if WORK_AUTHORIZATION_DOMAIN.search(field.label or ""):
        resolution = _resolve_work_authorization(field, answers)
        # Blank is never a false claim. "What visa type do you hold? (If
        # applicable)" is optional free text on the boards that ask it, and
        # parking a role over it would cost more than it protects.
        if resolution.parked and not field.required:
            return _skip(resolution.reason, "B0")
        return resolution

    rule = _resolve_rule(field, answers)
    if rule is not None:
        return rule

    # Tier C: /apply decides whether this is draftable free text or a "why us"
    # question. Either way nothing deterministic can fill it.
    return (_park("no rule matches this question", "C")
            if field.required else _skip("optional, no rule matches", "C"))
