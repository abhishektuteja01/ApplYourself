"""Resolve a rendered application field to the value that goes into it.

Deterministic: facts and questions from the answer bank
(`profile/application_answers.yaml`), plus structural rules for the blocks every
board shares. Nothing here judges, drafts or guesses — a field this module
cannot resolve comes back parked, and a parked field stops the submission.

Resolution order, keyed on the merged field's section and id before its prose:

    A   identity / education / employment / uploads -> the bank's fact blocks
    A2  eeoc / demographic                          -> your answer, else opt out
    B0  work authorization                          -> a "U.S. person" question from
                                                       us_person; the rest, the judge
    C   anything left                               -> the judge

Code answers only A, A2 and the U.S.-person question. Every other field parks
"for the judge" (skipped when optional); a judged entry for it renders through
`answer_question` and `resolve_fact_wording`, behind `plan.apply_override`.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta

from applyourself.apply.bank_schema import (
    Answers,
    Question,
    _norm_option,
    in_work_authorization_domain,
)
from applyourself.apply.places import (
    UNKNOWN_COUNTRY,
    _either_place,
    canonical_place,
    places_named,
)

# Re-exported: role_facts, score_filter and the CLIs read places through here.
__all__ = ["UNKNOWN_COUNTRY", "canonical_place", "places_named"]
from applyourself.apply.reconcile import MergedField
from applyourself.shared import clock

# The DOM ids the identity block fills, mapped to their config key. `location`
# renders as `candidate-location`; reconcile keeps the DOM id.
_IDENTITY_IDS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "preferred_name": "preferred_name",
    "email": "email",
    "phone": "phone",
    "candidate-location": "location",
    "location": "location",  # Lever's raw DOM name for the same concept
    "country": "country",
    "_systemfield_email": "email",  # Ashby's raw systemfield id
    "_systemfield_location": "location",
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

#: Every date this module writes. Verified live against a real board's picker,
#: where ISO silently misparsed by a day and DD/MM/YYYY swapped month and day.
#: No ATS declares a format; Lever's DOM placeholder reads MM/DD/YYYY.
DATE_FORMAT = "%m/%d/%Y"

# The self-identification form attests who filled it in, and both fields become
# required once the disability question is answered at all. Both values are
# known — the configured name and the clock.
_EEOC_SIGNATURE_IDS = frozenset(
    {
        "eeo[disabilitySignature]",
        "disability_signature",
    }
)
_EEOC_SIGNATURE_DATE_IDS = frozenset(
    {
        "eeo[disabilitySignatureDate]",
        "disability_signature_date",
    }
)

_DEMOGRAPHIC_IDS = {
    "gender": "gender",
    "eeo[gender]": "gender",
    "race": "race",
    "eeo[race]": "race",
    "hispanic_ethnicity": "hispanic_ethnicity",
    "veteran_status": "veteran_status",
    "eeo[veteran]": "veteran_status",
    "disability_status": "disability_status",
    "eeo[disability]": "disability_status",
}
# For the free-labelled demographic questions (Greenhouse's numeric ids), in
# order. `\bgender\b` so "transgender" is never the gender question, and a
# label naming race is the race question even when it also says "Hispanic".
_RACE_LABEL = re.compile(r"\brace\b|racial|ethnic", re.IGNORECASE)
_DEMOGRAPHIC_LABELS = (
    ("sexual_orientation", re.compile(r"sexual\s+orientation|\bsexuality\b", re.IGNORECASE)),
    ("veteran_status", re.compile(r"veteran|armed\s+forces|military\s+service", re.IGNORECASE)),
    ("disability_status", re.compile(r"disabilit", re.IGNORECASE)),
    ("hispanic_ethnicity", re.compile(r"hispanic|latin[oax]", re.IGNORECASE)),
    ("race", _RACE_LABEL),
    ("gender", re.compile(r"\bgender\b|\bsex\b", re.IGNORECASE)),
)

# Opt-out wordings, tried in order against the options the form renders. The
# flagged decline option comes first where a board marks one.
_OPT_OUT = (
    "Decline To Self Identify",
    "I don't wish to answer",
    "I do not wish to answer",
    "I do not want to answer",
    "Decline to self identify",
    "Decline to self-identify",
    "I prefer not to answer",
    "Prefer not to say",
    "I decline to self-identify for protected veteran status",
)

# "U.S. person" as ITAR/EAR defines it (citizen, permanent resident, refugee or
# asylee). Not the same question as work authorization — a time-limited status
# is authorized to work and is *not* a U.S. person. Answered only from an
# explicit `us_person`; any other export-control question parks for the judge,
# which may answer it only with a `us_person` fact.
_EXPORT_CONTROL = re.compile(
    r"u\.?\s?s\.?\s+person|export\s+control|deemed\s+export|\bitar\b|\bear\b\s+regulat",
    re.IGNORECASE,
)
# The one export-control shape code answers from `us_person`. Every other one
# ("does the deemed export rule affect you?") can flip polarity.
_US_PERSON_QUESTION = re.compile(r"u\.?\s?s\.?\s+person", re.IGNORECASE)

_YES_NO = {True: "Yes", False: "No"}
# A board that renders "Yes." / "No." — safe because `_pick_option` matches
# whole normalized labels.
_YES_NO_VARIANTS = {True: ("Yes", "Yes."), False: ("No", "No.")}


def _offers_bare_yes_no(options: tuple[str, ...]) -> bool:
    """Whether a bare "Yes"/"No" is on offer at all. When it is not, every
    option carries a claim past the polarity."""
    bare = {"yes", "no"}
    return any(_norm_option(o).rstrip(".") in bare for o in options)


@dataclass(frozen=True)
class Resolution:
    """What to do with one field. `fill` carries a value; `skip` leaves an
    optional field alone; `park` stops the whole submission; `defer` hands the
    field to plan.py, which owns the /tailor output dir."""

    action: str  # fill | skip | park | defer
    value: str | tuple[str, ...] | bool | None = None
    tier: str = ""
    reason: str = ""
    source: str = ""
    """What answered it: `judged:<id>` for a judged bank question,
    `override:<source>` for an `--answers` entry, `export_control` for that
    field. Empty where the tier is the whole story."""

    alternates: tuple[str, ...] = ()
    """Other spellings the read-back may accept for this value: a chooser with
    no option list at plan time gets the first candidate, and the board may
    settle on another one the user listed (an identity location's alternates)."""

    @property
    def parked(self) -> bool:
        return self.action == "park"


FOR_THE_JUDGE = "for the judge"


def _fill(value, tier: str, source: str = "") -> Resolution:
    return Resolution("fill", value=value, tier=tier, source=source)


def _skip(reason: str, tier: str = "") -> Resolution:
    return Resolution("skip", tier=tier, reason=reason)


def _park(reason: str, tier: str = "", source: str = "") -> Resolution:
    return Resolution("park", tier=tier, reason=reason, source=source)


# ---------------------------------------------------------------- resolving


def _pick_option(
    field: MergedField, candidates: tuple[str, ...], mode: str = "exact"
) -> str | None:
    """The first candidate the widget actually offers.

    With no option list — every DOM-only react-select — there is nothing to
    check against, so the first candidate goes through and fill.py's
    post-selection assert catches a string the widget rejects.

    `mode="contains"` matches a candidate anywhere inside an offered option
    ("Yes" inside "Yes, I am able and willing to work...").
    """
    if not field.options:
        return candidates[0] if candidates else None
    if mode == "contains":
        for candidate in candidates:
            cand_norm = _norm_option(candidate)
            if not cand_norm:
                continue
            for option in field.options:
                if cand_norm in _norm_option(option.label):
                    return option.label
        return None
    offered = {_norm_option(o.label): o.label for o in field.options}
    for candidate in candidates:
        hit = offered.get(_norm_option(candidate))
        if hit is not None:
            return hit
    return None


def match_option(field: MergedField, candidate: str) -> str | None:
    """The widget's own spelling of `candidate`, or None if it offers no such
    option. Returns the candidate unchanged when the field has no option list."""
    return _pick_option(field, (candidate,))


def _resolve_choice(
    field: MergedField,
    candidates: tuple[str, ...],
    tier: str,
    what: str,
    mode: str = "exact",
    source: str = "",
) -> Resolution:
    picked = _pick_option(field, candidates, mode=mode)
    if picked is None:
        offered = [o.label for o in field.options]
        return _park(
            f"{what}: none of {list(candidates)} is offered ({offered})", tier, source=source
        )
    return _fill((picked,) if field.multi else picked, tier, source=source)


def _pick_country(field: MergedField, value: str) -> str | None:
    """The country option, allowing for the dial code Greenhouse appends.

    `#country` renders inside `phone-input__country` — it is the phone dial-code
    selector, and its options read "United States +1". Exact first, then the
    one option that is the country followed by " +"; more than one is refused.
    """
    if not field.options:
        return value
    wanted = _norm_option(value)
    for option in field.options:
        if _norm_option(option.label) == wanted:
            return option.label
    hits = [o.label for o in field.options if _norm_option(o.label).startswith(f"{wanted} +")]
    return hits[0] if len(hits) == 1 else None


def _resolve_identity(field: MergedField, answers: Answers) -> Resolution | None:
    if field.id in FILE_IDS:
        return Resolution("defer", tier="A", reason=field.id)
    if field.id in ("full_name", "name", "_systemfield_name"):
        first, last = answers.identity["first_name"], answers.identity["last_name"]
        return _fill(f"{first} {last}".strip(), "A")
    key = _IDENTITY_IDS.get(field.id)
    if key is None:
        return None
    value = answers.identity[key]
    if field.id == "country":
        picked = _pick_country(field, value)
        if picked is None:
            return _park(
                f"identity.country: {value!r} matches none of {[o.label for o in field.options]}",
                "A",
            )
        return _fill(picked, "A")
    if field.kind == "react_select":
        candidates = _identity_candidates(field.id, value, answers, key)
        choice = _resolve_choice(field, candidates, "A", f"identity.{key}")
        return replace(choice, alternates=candidates) if choice.action == "fill" else choice
    return _fill(value, "A")


#: Ashby's location field is city-level on some boards and country-only on
#: others, and nothing in the payload says which.
_COUNTRY_FALLBACK_IDS = frozenset({"_systemfield_location"})


def _identity_candidates(
    field_id: str, value: str, answers: Answers, key: str = ""
) -> tuple[str, ...]:
    """The values to offer a chooser for this identity field, best first.
    Keyed on the DOM id, never the label: `identity.country` also feeds
    Greenhouse's phone dial-code widget."""
    if key == "location":
        alternates = answers.identity.get("location_alternates") or ()
        candidates = (value, *(a for a in alternates if a != value))
        if field_id in _COUNTRY_FALLBACK_IDS:
            candidates = (*candidates, answers.identity["country"])
        return candidates
    if field_id in _COUNTRY_FALLBACK_IDS:
        return (value, answers.identity["country"])
    return (value,)


def _resolve_repeating(
    field: MergedField,
    block: dict[str, str] | None,
    ids: dict[str, str],
    suffix: re.Pattern,
    config_key: str,
) -> Resolution:
    match = suffix.match(field.id)
    if match is None:
        return _park(f"{config_key}: unrecognized field id {field.id!r}", "A")
    if match.group("n") != "0":
        return (
            _park(f"{config_key}: a second entry is required", "A")
            if field.required
            else _skip("only entry 0 is filled", "A")
        )
    if block is None:
        missing = (
            f"{config_key}: this board renders an {config_key} block and "
            f"the answer bank has no {config_key}: section"
        )
        return _park(missing, "A") if field.required else _skip(missing, "A")
    base = match.group("base")
    if base == "current-role":
        return _fill(bool(block.get("current_role")), "A")
    key = ids.get(base)
    if key is None:
        return _park(f"{config_key}: unrecognized field id {field.id!r}", "A")
    value = block.get(key)
    if value is None:
        return (
            _park(f"{config_key}.{key}: required by this board, not set", "A")
            if field.required
            else _skip(f"{config_key}.{key} not set", "A")
        )
    if field.kind == "react_select":
        alternates = block.get(f"{key}_alternates") or ()
        candidates = (str(value), *(a for a in alternates if a != value))
        return _resolve_choice(field, candidates, "A", f"{config_key}.{key}")
    return _fill(str(value), "A")


def _today() -> date:
    """Indirected so a test can pin the date without freezing the clock."""
    return clock.today()


def demographic_topic(field: MergedField) -> str:
    """Which `DEMOGRAPHIC_TOPICS` entry a demographic field asks, or "": by the
    standard EEOC ids first, else by a free label."""
    if field.id in _DEMOGRAPHIC_IDS:
        return _DEMOGRAPHIC_IDS[field.id]
    label = field.label or ""
    for topic, pattern in _DEMOGRAPHIC_LABELS:
        if pattern.search(label):
            if topic == "hispanic_ethnicity" and _RACE_LABEL.search(label):
                continue
            return topic
    return ""


_PREFIX_MIN = 4


def _pick_own_answer(field: MergedField, candidates: tuple[str, ...]) -> str | None:
    """The first candidate the board offers, exactly or as the start of a longer
    option ("Asian" in "Asian (Not Hispanic or Latino)" or "Asian: Individuals
    with origins ..."), never inside another word ("Asian" in "South Asian")."""
    picked = _pick_option(field, candidates)
    if picked is not None:
        return picked
    for candidate in candidates:
        want = _norm_option(candidate)
        if len(want) < _PREFIX_MIN:
            continue  # "No" must never pick "No, I don't wish to answer"
        for option in field.options:
            have = _norm_option(option.label)
            if have.startswith(want) and not have[len(want) : len(want) + 1].isalnum():
                return option.label
    return None


def _resolve_eeoc(field: MergedField, answers: Answers) -> Resolution:
    """EEOC and demographic questions alike. A topic `demographics.answers`
    states gets your answer, in the first spelling the board offers; anything
    else (or a board offering none of your spellings) takes the flagged decline
    option, else an opt-out wording, else parks (required) or skips."""
    if field.id in _EEOC_SIGNATURE_IDS:
        first, last = answers.identity["first_name"], answers.identity["last_name"]
        return _fill(f"{first} {last}".strip(), "A2")
    if field.id in _EEOC_SIGNATURE_DATE_IDS:
        return _fill(_today().strftime(DATE_FORMAT), "A2")
    topic = demographic_topic(field)
    own = answers.demographic_answers.get(topic) if topic else None
    if own and field.options:
        picked = _pick_own_answer(field, own)
        if picked is not None:
            return _fill((picked,) if field.multi else picked, "A2", source=f"demographics:{topic}")
    picked = None
    if field.options:
        flagged = tuple(o.label for o in field.options if o.decline_to_answer)
        picked = _pick_option(field, flagged + _OPT_OUT)
    if picked is None:
        return (
            _park(f"{field.id}: no opt-out option offered", "A2")
            if field.required
            else _skip("no opt-out option offered", "A2")
        )
    reason = f"none of demographics.answers.{topic} {list(own)} is offered; declined" if own else ""
    return Resolution("fill", value=(picked,) if field.multi else picked, tier="A2", reason=reason)


def _yes_no(field: MergedField, value: bool, tier: str = "B0") -> Resolution | None:
    """A bool answered in the widget's own words, or None if it offers no bare
    Yes/No."""
    if not field.options:
        return _fill(_YES_NO[value], tier)
    picked = _pick_option(field, _YES_NO_VARIANTS[value])
    if picked is None:
        return None
    return _fill((picked,) if field.multi else picked, tier)


@functools.cache
def _us_place_patterns() -> tuple[re.Pattern, re.Pattern]:
    """A US state, DC or a US-like territory by name ("California", "Puerto
    Rico", "U.S. Virgin Islands"), and a state code after a comma ("San Jose,
    CA"). A name that is also a country ("Georgia") is left out."""
    import pycountry

    from applyourself.shared.countries import canonical_country
    from applyourself.shared.location.places import US_LIKE

    subs = [
        (code, s.name)
        for s in pycountry.subdivisions.get(country_code="US")
        if (code := s.code.split("-")[1]) in US_LIKE or s.type != "Outlying area"
    ]
    names = [
        re.escape(name)
        for code, name in subs
        if "," not in name and (code in US_LIKE or not canonical_country(name))
    ]
    names.append(r"u\.?s\.?\s+virgin\s+islands|usvi")
    codes = "|".join(code for code, _ in subs)
    return (
        re.compile(r"(?<![\w.])(?:" + "|".join(names) + r")(?![\w])", re.IGNORECASE),
        re.compile(r",\s*(?:" + codes + r")\b"),
    )


def _question_countries(label: str, answers: Answers) -> list[str]:
    """The countries a work-authorization label names, a US state or
    territory counting as the United States; else the job's country."""
    named = places_named(label)
    by_name, by_code = _us_place_patterns()
    # A state code only in mixed case: in capitals ", OR" is a word.
    us = by_name.search(label or "") or (not label.isupper() and by_code.search(label or ""))
    if us and "United States" not in named:
        named.append("United States")
    return named or [answers.country]


def _one_value(label: str, answers: Answers, fact) -> tuple[bool | None, str]:
    """The fact across every country the label names: any of them for "the US
    or Canada", all of them otherwise ("the US and the UK")."""
    countries = _question_countries(label, answers)
    values = [fact(answers.facts_for(c)) for c in countries]
    if len(countries) > 1 and _either_place(label):
        return any(values), ""
    return all(values), ""


def resolve_fact_wording(field: MergedField, answers: Answers, judged: dict) -> Resolution:
    """A judged `{fact, yes_when}`: Yes exactly when the fact it names equals
    `yes_when` for the question's country, so the answer follows your facts."""
    fact = judged["fact"]
    source = f"fact_wording:{fact}"
    if fact == "us_person":
        if answers.us_person == "park":
            return _park(
                'asks whether you are a "U.S. person". Set '
                "work_authorization.us_person to answer it",
                "B0",
                source=source,
            )
        value: bool | None = answers.us_person == "yes"
    else:
        if UNKNOWN_COUNTRY in _question_countries(field.label, answers):
            return _park(
                "work authorization: the job's country could not be read from "
                "its location, and this question depends on it",
                "B0",
                source=source,
            )
        value, _ = _one_value(field.label, answers, lambda f: getattr(f, fact))
    answered = _yes_no(field, value == judged["yes_when"])
    if answered is None:
        return _park(f"{source}: the board offers no bare Yes/No", "B0", source=source)
    return Resolution("fill", answered.value, "B0", source=source)


def _resolve_export_control(field: MergedField, answers: Answers) -> Resolution:
    """A "U.S. person" question from an explicit `us_person`; any other
    export-control question, or an unset `us_person`, parks for the judge."""
    if not _US_PERSON_QUESTION.search(field.label or "") or answers.us_person == "park":
        return _park(
            "export control: for the judge (a us_person fact only)", "B0", "export_control"
        )
    answered = _yes_no(field, answers.us_person == "yes")
    if answered is not None:
        return Resolution("fill", answered.value, "B0", source="export_control")
    return _park("export control: no bare Yes/No offered", "B0", source="export_control")


# JobSpy-aggregator sources: the listing genuinely was seen there.
_AGGREGATOR_SOURCE_LABELS = {"linkedin": "LinkedIn", "indeed": "Indeed"}
# Scraped straight off the company's own board.
_OWN_BOARD_SOURCES = frozenset({"greenhouse", "lever", "ashby"})
_OWN_BOARD_OPTION_HINTS = ("career site", "careers page", "company website", "company career site")


def _how_heard_candidates(answers: Answers) -> tuple[str, ...]:
    source = answers.job_source.strip().lower()
    if source in _AGGREGATOR_SOURCE_LABELS:
        return (_AGGREGATOR_SOURCE_LABELS[source],)
    if source in _OWN_BOARD_SOURCES:
        return _OWN_BOARD_OPTION_HINTS
    return ()


def _computed(question: Question, field: MergedField, answers: Answers, source: str) -> Resolution:
    country = answers.country
    if country == UNKNOWN_COUNTRY and question.computed in ("salary", "start_date"):
        return _park(
            f"{question.computed}: the job's country could not be read from its location",
            "B",
            source=source,
        )
    if question.computed == "start_date":
        days = answers.facts_for(country).start_in_days
        if days is None:
            return _park(
                f"start date: no start_in_days set for {country} (work_authorization.countries)",
                "B",
                source=source,
            )
        return _fill((_today() + timedelta(days=days)).strftime(DATE_FORMAT), "B", source=source)

    if question.computed == "how_heard":
        own = _how_heard_candidates(answers)
        if own and (field.options or field.kind == "react_select"):
            picked = _pick_option(field, own, mode="contains")
            if picked is not None:
                return _fill((picked,) if field.multi else picked, "B", source=source)
        if not question.answers:
            return _park("how heard: no answer for this source", "B", source=source)
        if field.options or field.kind == "react_select":
            return _resolve_choice(
                field, question.answers, "B", question.id, mode=question.mode, source=source
            )
        return _fill(question.answers[0], "B", source=source)

    # salary. A Yes/No widget asks whether the posted range is acceptable, not
    # for a figure — the literal fallback answers that.
    options = tuple(o.label for o in field.options)
    if options and _offers_bare_yes_no(options):
        return _resolve_choice(
            field, question.answers, "B", question.id, mode=question.mode, source=source
        )
    if answers.salary_figure is None:
        return _park(
            f"salary: no figure for a role in {country} (its work_authorization row's salary)",
            "B",
            source=source,
        )
    figure = round(answers.salary_figure)
    candidates = (str(figure), f"${figure:,}")
    if field.options or field.kind == "react_select":
        return _resolve_choice(field, candidates, "B", question.id, source=source)
    return _fill(str(figure), "B", source=source)


def answer_question(
    question: Question, field: MergedField, answers: Answers, source: str
) -> Resolution:
    """`question`'s answer rendered for `field`: computed, role file, a board
    option (on a multi-select, every candidate the board offers) or its first
    spelling, tagged `source`."""
    if question.only_when_required and not field.required:
        return _skip(f"{question.id} is only_when_required and this field is optional", "B")
    if field.kind == "file":
        return (
            _park("a question cannot answer a file upload", "B", source=source)
            if field.required
            else _skip("optional file upload, no question can fill it", "B")
        )
    if question.computed:
        return _computed(question, field, answers, source)
    if question.role_file:
        text = answers.company_answers.get(question.role_file)
        if not text:
            return _park(
                f"{question.id}: company_answers.yaml has no {question.role_file} "
                f"for this role (run /company-answers)",
                "B",
                source=source,
            )
        return _fill(text, "B", source=source)
    if field.multi and field.options:
        picked = tuple(
            dict.fromkeys(
                p for c in question.answers if (p := _pick_option(field, (c,), question.mode))
            )
        )
        if picked:
            return _fill(picked, "B", source=source)
    if field.options or field.kind == "react_select":
        return _resolve_choice(
            field, question.answers, "B", question.id, mode=question.mode, source=source
        )
    return _fill(question.answers[0], "B", source=source)


def _own_demographic(field: MergedField, answers: Answers) -> Resolution | None:
    """A demographic question asked outside the EEOC section (Ashby asks them
    as ordinary questions): your own answer when the board offers one of your
    spellings. Never a decline here; anything else goes on to the judge."""
    topic = demographic_topic(field)
    own = answers.demographic_answers.get(topic) if topic else None
    picked = _pick_own_answer(field, own) if own and field.options else None
    if picked is None:
        return None
    return _fill((picked,) if field.multi else picked, "A2", source=f"demographics:{topic}")


def resolve(field: MergedField, answers: Answers) -> Resolution:
    """Resolve one reconciled field. Never raises on content — an unanswerable
    field comes back parked (required) or skipped (optional)."""
    if field.section in ("eeoc", "demographic"):
        return _resolve_eeoc(field, answers)
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

    own = _own_demographic(field, answers)
    if own is not None:
        return own

    label = field.label or ""
    if in_work_authorization_domain(label):
        if _EXPORT_CONTROL.search(label):
            resolution = _resolve_export_control(field, answers)
        else:
            resolution = _park(FOR_THE_JUDGE, "B0")
    else:
        resolution = _park(FOR_THE_JUDGE, "C")
    # Blank is never a false claim: an optional field the judge does not
    # answer is left alone.
    if resolution.parked and not field.required:
        return Resolution(
            "skip", tier=resolution.tier, reason=resolution.reason, source=resolution.source
        )
    return resolution
