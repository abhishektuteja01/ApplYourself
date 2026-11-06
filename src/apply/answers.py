"""Resolve a rendered application field to the value that goes into it.

Deterministic: facts and questions from the answer bank
(`profile/application_answers.yaml`), plus structural rules for the blocks every
board shares. Nothing here judges, drafts or guesses — a field this module
cannot resolve comes back parked, and a parked field stops the submission.

Resolution order, keyed on the merged field's section and id before its prose:

    A   identity / education / employment / uploads -> the bank's fact blocks
    A2  eeoc / demographic                          -> opt out, structurally
    B0  work authorization                          -> work_authorization, per country
    B   everything else                             -> questions: exact wording
                                                       first, then keyword
    C   anything left                               -> park if required, skip if not

Work authorization is its own block because one answer cannot serve both halves
of it: "are you authorized to work" and "will you require sponsorship" are Yes
and Yes for an F-1 OPT holder, and Yes and No for a citizen. And it is per
country: the same person is authorized in one country and not in another.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import yaml

from src.apply.reconcile import MergedField
from src.shared import clock, paths

EXAMPLE_NAME = "application_answers.example.yaml"

SCHEMA_VERSION = 2

IDENTITY_KEYS = (
    "first_name",
    "last_name",
    "preferred_name",
    "email",
    "phone",
    "location",
    "country",
)
#: Optional, list-valued. The same location as a board's own taxonomy words it —
#: one ATS abbreviates state and country where another spells both out, and an
#: exact-match chooser takes neither for the other. Tried in order after
#: `location`.
IDENTITY_LIST_KEYS = ("location_alternates",)
EDUCATION_KEYS = ("school", "degree", "discipline", "start_year", "end_year")
EDUCATION_OPTIONAL_KEYS = ("start_month", "end_month")
# How a board's own taxonomy words this school. Ashby's combobox stacks
# name/country/domain into one option label and offers several same-named
# schools, so an exact spelling is the only safe match.
EDUCATION_LIST_KEYS = ("school_alternates",)
EMPLOYMENT_KEYS = (
    "company_name",
    "title",
    "start_month",
    "start_year",
    "end_month",
    "end_year",
)
# Flags on the employment block, parsed as booleans rather than strings.
EMPLOYMENT_FLAGS = ("current_role", "only_when_required")

TOP_LEVEL_KEYS = (
    "schema_version",
    "identity",
    "work_authorization",
    "education",
    "employment",
    "demographics",
    "claims",
    "questions",
    "fact_wordings",
    # Not an answer: whether an optional cover-letter upload gets a letter.
    "cover_letter",
    # Not an answer: which browser drives the form. Owned and validated by
    # src/apply/browser.py.
    "browser",
)

# A keyword this short matches almost any label. Punctuation is stripped before
# matching, so `C++` normalizes to the single character `c`.
MIN_KEYWORD_LENGTH = 3

# Answers a question may compute rather than state.
COMPUTED = ("salary", "start_date", "how_heard")

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

#: `cover_letter:` values. `when_offered` attaches a letter wherever the board
#: has a cover-letter upload; `when_required` only where it requires one.
COVER_LETTER_POLICIES = ("when_required", "when_offered")

# The demographic topics `demographics.answers` may state. Any other
# demographic question (sexual orientation, transgender, pronouns) opts out.
DEMOGRAPHIC_TOPICS = ("gender", "race", "hispanic_ethnicity", "veteran_status", "disability_status")
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
# explicit `us_person`, and no override may touch it.
_EXPORT_CONTROL = re.compile(
    r"u\.?\s?s\.?\s+person|export\s+control|deemed\s+export|\bitar\b|\bear\b\s+regulat",
    re.IGNORECASE,
)
# The one export-control shape `us_person` answers. Every other one ("does the
# deemed export rule affect you?") can flip polarity, so it always parks.
_US_PERSON_QUESTION = re.compile(r"u\.?\s?s\.?\s+person", re.IGNORECASE)

# Anything work-authorization shaped. A question keyword or wording that lands
# in here is a load error; a label that lands in here is answered by the
# work-authorization resolver, or parked.
WORK_AUTHORIZATION_DOMAIN = re.compile(
    r"sponsor|visa|work\s+authori[sz]|authou?ri[sz]\w*\s+to\s+work|citizenship|right\s+to\s+work"
    r"|eligib\w*\s+to\s+(?:work|live)|work\s+eligib|employment\s+eligib"
    r"|work\s+status|nationalit|\bcitizen\b|optional\s+practical\s+training"
    r"|authou?ri[sz]\w*\s+\w{0,3}\s*to\s+work"
    r"|country\s+of\s+origin"
    r"|u\.?\s?s\.?\s+person|export\s+control|deemed\s+export|\bitar\b",
    re.IGNORECASE,
)
# Caught by the domain above but NOT work authorization — these fall through to
# the question bank. Positive shape match: the relocation-cities checkbox names
# "visa sponsorship" in its own preamble.
_NOT_WORK_AUTH = re.compile(
    r"relocation\s+support"
    r"|at\s+least\s+18\s+years"
    r"|security\s+clearance|clearance\s+eligib"
    r"|notetaker|ai\s+interview\s+tool"
    r"|privacy\s+notice|privacy\s+policy"
    r"|hereby\s+certify|knowingly\s+withheld",
    re.IGNORECASE,
)
_AUTHORIZED_FAMILY = re.compile(
    r"authou?ri[sz]\w*\s+to\s+work|legally\s+authou?ri[sz]"
    r"|right\s+to\s+work|eligib\w*\s+to\s+(?:work|live)"
    r"|(?:have|hold)\s+(?:\S+\s+){0,3}work\s+authori[sz]"
    r"|authou?ri[sz]\w*\s+\w{0,3}\s*to\s+work",
    re.IGNORECASE,
)
# "Can you provide proof of your authorization?" — answerable by anyone who is
# authorized at all.
_PROOF_FAMILY = re.compile(
    r"(?:proof|evidence|documentation)\s+of\s+(?:\w+\s+){0,3}"
    r"(?:authoriz|eligib|right\s+to\s+work)"
    r"|able\s+to\s+provide\s+(?:\w+\s+){0,3}(?:proof|documentation)",
    re.IGNORECASE,
)
_SPONSORSHIP_FAMILY = re.compile(
    r"(?:requir|need)\w*[^?]*sponsor|sponsor\w*[^?]*(?:requir|need)"
    r"|(?:requir|need)\w*\s+(?:\S+\s+){0,4}(?:work\s+)?visa"
    r"|(?:requir|need)\w*\s+(?:\S+\s+){0,3}work\s+authori[sz]",
    re.IGNORECASE,
)
# "Are you able to work without sponsorship?" / "Do you not need sponsorship?"
# invert the answer. Never guess them.
_SPONSORSHIP_NEGATED = re.compile(
    r"without\s+(?:\w+\s+){0,2}sponsor|not\s+(?:\w+\s+){0,2}(?:requir|need)\w*[^?]*sponsor",
    re.IGNORECASE,
)
# When a label reads as both families, which one is actually being asked:
# "Will you require sponsorship ... to maintain authorization to work in the US?"
_SPONSORSHIP_GOVERNS = re.compile(
    r"^\s*(?:will|do|would)\s+you\s+(?:\S+\s+){0,8}"
    r"(?:requir|need)\w*\s+(?:\S+\s+){0,4}sponsor"
    r"|(?:requir|need)\w*\s+(?:\w+\s+){0,3}sponsor\w*\s+(?:\w+\s+){0,4}"
    r"(?:to|for)\s+(?:maintain|retain|extend|continue|obtain|remain|commence)",
    re.IGNORECASE,
)
# An authorization question that narrows the scope — permanence,
# employer-independence, freedom from sponsorship. Answered from the country's
# `unrestricted` fact. "for any employer" is deliberately NOT a qualifier: OPT
# is not employer-tied the way an H-1B is.
_AUTHORIZED_QUALIFIED = re.compile(
    r"permanent\w*"
    r"|ongoing"
    r"|indefinite\w*"
    r"|unrestricted"
    r"|without\s+(?:\S+\s+){0,8}(?:sponsor|restrict)"
    r"|now\s+or\s+in\s+the\s+future"
    r"|no\s+(?:\w+\s+){0,2}restrict",
    re.IGNORECASE,
)
# The qualifier offered as one alternative rather than required ("on a permanent
# or temporary basis") — the narrow reading is not the question, so these park.
_QUALIFIER_OFFERED_AS_ALTERNATIVE = re.compile(
    r"permanent\w*\s+or\s+\w+"
    r"|\w+\s+or\s+permanent\w*"
    r"|with\s+or\s+without",
    re.IGNORECASE,
)
# A request to ELABORATE, not a yes/no. Answered from the country's `followup`
# text in a free-text box, else parked.
_FOLLOWUP = re.compile(
    r"if\s+you\s+(?:answered|selected)"
    r"|please\s+(?:share|provide|specify|elaborate|explain)"
    r"|(?:what|which)\s+(?:\w+\s+){0,2}(?:sponsorship|visa|permit)\s+"
    r"(?:would|do|will)"
    r"|(?:what|which)\s+(?:\S+\s+){0,2}countr",
    re.IGNORECASE,
)
# Asks WHICH status you hold, rather than yes/no. Answered from the country's
# `status_label` (free text) or a known-true option (a picker).
_STATUS_DISCLOSURE = re.compile(
    r"^\s*work\s+(?:status|eligibility|authori[sz]\w*\s+status)"
    r"|(?:confirm|indicate|update|select)\s+(?:\S+\s+){0,6}authori[sz]\w*\s+status"
    r"|(?:what|which)\s+is\s+your\s+(?:visa|status)"
    r"|(?:what|which)\s+type\s+of\s+(?:work\s+)?(?:authori[sz]\w*|visa)"
    r"|(?:update|confirm|indicate|select)\s+(?:\w+\s+){0,3}employ\w*\s+status"
    r"|currently\s+in\s+a\s+period\s+of",
    re.IGNORECASE,
)
# Nationality and citizenship are a COUNTRY, not a visa status.
_NATIONALITY = re.compile(
    r"nationalit"
    r"|citizenship\s+in|hold\s+citizenship|citizenship\s+of"
    r"|(?:dual|second|another|other|additional)\s+citizenship"
    r"|(?:what|which)\s+is\s+your\s+citizenship"
    r"|are\s+you\s+(?:currently\s+)?an?\s+\w+\s+citizen"
    r"|citizen\s+of\s+a\s+country"
    r"|country\s+of\s+origin",
    re.IGNORECASE,
)
# A second citizenship is a different fact from the primary one.
_SECOND_NATIONALITY = re.compile(
    r"second\s+(?:country|nationality|citizenship)"
    r"|another\s+(?:country|nationality|citizenship)"
    r"|other\s+citizenship|dual\s+citizenship|additional\s+citizenship",
    re.IGNORECASE,
)
_NONE_CANDIDATES = ("None", "No", "N/A", "Not applicable")

# Countries a label can name come from discovery's vocabulary (pycountry's
# names plus its aliases), so a place is spelled one way across the pipeline.
# Regions have no country of their own: they answer from `default` unless the
# bank gives them a row.
REGIONS: dict[str, str] = {
    "European Union": r"european\s+union|\beu\b|\beea\b|\befta\b|\beurope\b|\bemea\b",
    "Latin America": r"latin\s+america|\blatam\b|south\s+america|central\s+america",
    "Asia Pacific": r"\bapac\b|asia[\s-]pacific",
}
# Places whose names contain a country's: masked before any country is looked
# for, so "New Mexico" is never Mexico and "Bank of America" never the US.
_NOT_COUNTRIES = (
    "new south wales",
    "new england",
    "papua new guinea",
    "bank of america",
    "north america",
    "latin america",
    "south america",
    "central america",
)
_SHORT_NAMES = {
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "uae": "United Arab Emirates",
}
_ALIASES = {
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "united states of america": "United States",
    "america": "United States",
    "holland": "Netherlands",
    "the netherlands": "Netherlands",
    "turkiye": "Turkey",
    "czech republic": "Czechia",
    "south korea": "Korea, Republic of",
    "korea": "Korea, Republic of",
    "russia": "Russian Federation",
    "vietnam": "Viet Nam",
    "taiwan": "Taiwan, Province of China",
    "iran": "Iran, Islamic Republic of",
    "bolivia": "Bolivia, Plurinational State of",
    "venezuela": "Venezuela, Bolivarian Republic of",
    "tanzania": "Tanzania, United Republic of",
    "moldova": "Moldova, Republic of",
    "syria": "Syrian Arab Republic",
    "laos": "Lao People's Democratic Republic",
    "republic of ireland": "Ireland",
}
# Country names that are also ordinary words or US places in a form label.
_NOT_A_COUNTRY_WORD = frozenset(
    {
        "jersey",
        "georgia",
        "chad",
        "jordan",
        "guinea",
        "niger",
        "turkey",
        "man",
        "norfolk island",
        "christmas island",
    }
)
_OR_BETWEEN = re.compile(r"\bor\b|/", re.IGNORECASE)
#: The job's country when its location names a place nothing could read.
UNKNOWN_COUNTRY = "(unknown)"


def _fold_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    )


@functools.lru_cache(maxsize=1)
def _vocabulary() -> tuple[dict[str, str], tuple, re.Pattern, tuple]:
    import pycountry

    names: dict[str, str] = {}
    for c in pycountry.countries:
        for attr in ("name", "common_name", "official_name"):
            value = getattr(c, attr, "")
            if value:
                names[_fold_accents(value).casefold()] = c.name
    names = {k: v for k, v in names.items() if k not in _NOT_A_COUNTRY_WORD}
    names["turkey"] = "Turkey"  # the country name itself, once "Turkiye" is folded
    names.update(_ALIASES)
    names.update(_SHORT_NAMES)
    us_states = [s.name.casefold() for s in pycountry.subdivisions.get(country_code="US")]
    masked = sorted({*_NOT_COUNTRIES, *us_states}, key=len, reverse=True)
    codes = "|".join(
        sub.code.split("-")[1] for sub in pycountry.subdivisions.get(country_code="US")
    )
    mask = (
        re.compile(
            r"(?<![\w.])(?:" + "|".join(re.escape(m) for m in masked) + r")(?![\w])", re.IGNORECASE
        ),
        # "Columbia, MD", "Norfolk, VA": a place and a US state code.
        re.compile(r"\b[A-Z][\w.' -]{1,30},\s*(?:" + codes + r")\b"),
        # "Jersey City", "Mexico City": a city, not the country.
        re.compile(r"\b\w+\s+City\b"),
    )
    ordered = sorted((k for k in names if k != "us"), key=len, reverse=True)
    country = re.compile(
        r"(?<![\w.])(?:" + "|".join(re.escape(k) for k in ordered) + r")(?![\w])", re.IGNORECASE
    )
    regions = tuple((name, re.compile(p, re.IGNORECASE)) for name, p in REGIONS.items())
    return names, mask, country, regions


def canonical_place(text: str) -> str:
    """The canonical name for a country or region, or "" when it names neither."""
    raw = " ".join((text or "").split())
    if raw.casefold() == "default":
        return "default"
    names, _, _, regions = _vocabulary()
    key = _fold_accents(raw).casefold()
    if key in names:
        return names[key]
    if key.startswith("the ") and key[4:] in names:
        return names[key[4:]]
    for name, pattern in regions:
        if raw.casefold() == name.casefold() or pattern.fullmatch(raw):
            return name
    if raw == "US":
        return "United States"
    named = places_named(raw)
    return named[0] if len(named) == 1 else ""


def _place_hits(label: str) -> list[tuple[int, int, str]]:
    """(start, end, place) for every place in the accent-folded label."""
    names, mask, country, regions = _vocabulary()
    folded = _fold_accents(label)
    lowered = folded
    for pattern in mask:
        lowered = pattern.sub(lambda m: " " * len(m.group(0)), lowered)
    hits = [(m.start(), m.end(), names[m.group(0).casefold()]) for m in country.finditer(lowered)]
    hits += [
        (m.start(), m.end(), name)
        for name, pattern in regions
        for m in [pattern.search(folded)]
        if m
    ]
    # "US" in capitals only; in a label written all in capitals, only after "THE".
    us = r"(?<=\bTHE )US(?![\w])" if folded.isupper() else r"(?<![\w.])US(?![\w])"
    hits += [(m.start(), m.end(), "United States") for m in re.finditer(us, lowered)]
    return sorted(hits)


def places_named(label: str) -> list[str]:
    """Every country or region a label names, in order, each once."""
    seen: list[str] = []
    for _, _, name in _place_hits(label):
        if name not in seen:
            seen.append(name)
    return seen


def _either_place(label: str) -> bool:
    """Whether the label offers its places as alternatives: an "or" or a "/"
    between two of them ("the US or Canada", "US/Canada"), not anywhere ("now
    or in the future")."""
    hits = _place_hits(label)
    folded = _fold_accents(label)
    return any(_OR_BETWEEN.search(folded[a[1] : b[0]]) for a, b in pairwise(hits) if a[2] != b[2])


# Region membership for "are you a citizen of a country in <region>?" yes/no
# questions. Extend as real boards surface a region this doesn't cover.
_SOUTH_ASIA = frozenset(
    {
        "afghanistan",
        "bangladesh",
        "bhutan",
        "india",
        "maldives",
        "nepal",
        "pakistan",
        "sri lanka",
    }
)
_SOUTHEAST_ASIA = frozenset(
    {
        "brunei",
        "cambodia",
        "timor-leste",
        "indonesia",
        "laos",
        "malaysia",
        "myanmar",
        "philippines",
        "singapore",
        "thailand",
        "vietnam",
    }
)
_EAST_ASIA = frozenset(
    {
        "china",
        "hong kong",
        "japan",
        "macau",
        "mongolia",
        "north korea",
        "south korea",
        "taiwan",
    }
)
_CENTRAL_ASIA = frozenset(
    {
        "kazakhstan",
        "kyrgyzstan",
        "tajikistan",
        "turkmenistan",
        "uzbekistan",
    }
)
_WESTERN_ASIA = frozenset(
    {
        "armenia",
        "azerbaijan",
        "bahrain",
        "cyprus",
        "georgia",
        "iraq",
        "israel",
        "jordan",
        "kuwait",
        "lebanon",
        "oman",
        "palestine",
        "qatar",
        "saudi arabia",
        "syria",
        "turkey",
        "united arab emirates",
        "yemen",
    }
)
_ASIA = _SOUTH_ASIA | _SOUTHEAST_ASIA | _EAST_ASIA | _CENTRAL_ASIA | _WESTERN_ASIA
_EU = frozenset(
    {
        "austria",
        "belgium",
        "bulgaria",
        "croatia",
        "cyprus",
        "czechia",
        "denmark",
        "estonia",
        "finland",
        "france",
        "germany",
        "greece",
        "hungary",
        "ireland",
        "italy",
        "latvia",
        "lithuania",
        "luxembourg",
        "malta",
        "netherlands",
        "poland",
        "portugal",
        "romania",
        "slovakia",
        "slovenia",
        "spain",
        "sweden",
    }
)
_EFTA = frozenset({"iceland", "liechtenstein", "norway", "switzerland"})

# Matched as whole words, longest first, so "south asia" is tried before "asia"
# and "asia" never matches inside "asia-pacific" or "eurasia".
_NATIONALITY_REGIONS: dict[str, frozenset[str]] = {
    "eu/efta": _EU | _EFTA,
    "eu / efta": _EU | _EFTA,
    "european union": _EU,
    "southeast asia": _SOUTHEAST_ASIA,
    "south-east asia": _SOUTHEAST_ASIA,
    "south east asia": _SOUTHEAST_ASIA,
    "south asia": _SOUTH_ASIA,
    "southern asia": _SOUTH_ASIA,
    "east asia": _EAST_ASIA,
    "eastern asia": _EAST_ASIA,
    "central asia": _CENTRAL_ASIA,
    "asia": _ASIA,
}
_REGION_PATTERNS = tuple(
    (re.compile(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])"), members)
    for phrase, members in sorted(
        _NATIONALITY_REGIONS.items(), key=lambda kv: len(kv[0]), reverse=True
    )
)
# Demonym -> country, for "Are you currently an Indonesian citizen?".
_DEMONYMS: dict[str, str] = {
    "indonesian": "indonesia",
    "indian": "india",
    "pakistani": "pakistan",
    "bangladeshi": "bangladesh",
    "sri lankan": "sri lanka",
    "nepali": "nepal",
    "bhutanese": "bhutan",
    "afghan": "afghanistan",
    "filipino": "philippines",
    "malaysian": "malaysia",
    "singaporean": "singapore",
    "thai": "thailand",
    "vietnamese": "vietnam",
    "burmese": "myanmar",
    "cambodian": "cambodia",
    "laotian": "laos",
    "bruneian": "brunei",
    "chinese": "china",
    "japanese": "japan",
    "mongolian": "mongolia",
    "taiwanese": "taiwan",
    "kazakh": "kazakhstan",
    "uzbek": "uzbekistan",
    "turkish": "turkey",
    "israeli": "israel",
    "emirati": "united arab emirates",
    "saudi": "saudi arabia",
}
# Other spellings of a nationality, folded before any comparison, so "Czech
# Republic" and "Czechia" can never answer the same question two ways.
_COUNTRY_ALIASES = {
    "czech republic": "czechia",
    "holland": "netherlands",
    "the netherlands": "netherlands",
    "uae": "united arab emirates",
    "türkiye": "turkey",
    "turkiye": "turkey",
    "usa": "united states",
    "united states of america": "united states",
    "us": "united states",
    "uk": "united kingdom",
    "great britain": "united kingdom",
    "republic of korea": "south korea",
    "korea": "south korea",
}


def _country_key(name: str) -> str:
    key = _norm_option(name)
    return _COUNTRY_ALIASES.get(key, key)


def _nationality_region_match(label_norm: str, nationalities: tuple[str, ...]) -> bool | None:
    """Whether any of your nationalities is inside the region/country a yes-no
    label names, or None if the label names neither — the caller parks."""
    keys = {_country_key(n) for n in nationalities}
    for pattern, members in _REGION_PATTERNS:
        if pattern.search(label_norm):
            return bool(keys & members)
    for demonym, country in _DEMONYMS.items():
        if re.search(rf"(?<![\w-]){re.escape(demonym)}(?![\w-])", label_norm):
            return country in keys
    return None


_YES_NO = {True: "Yes", False: "No"}
# A board that renders "Yes." / "No." — safe because `_pick_option` matches
# whole normalized labels.
_YES_NO_VARIANTS = {True: ("Yes", "Yes."), False: ("No", "No.")}
# Option labels too generic to carry a work-authorization claim of their own.
_BARE_OPTIONS = frozenset({"yes", "no", "yes.", "no.", "other", "n/a"})

WORK_AUTH_CATEGORIES = (
    "plain",
    "qualified",
    "alternation",
    "sponsorship",
    "compound_option",
    "status_disclosure",
    "nationality",
    "export_control",
    "proof",
    "followup",
    "not_work_auth",
)


def _offers_bare_yes_no(options: tuple[str, ...]) -> bool:
    """Whether a bare "Yes"/"No" is on offer at all. When it is not, every
    option carries a claim past the polarity."""
    bare = {"yes", "no"}
    return any(_norm_option(o).rstrip(".") in bare for o in options)


def classify_work_authorization(label: str, options: tuple[str, ...] = ()) -> str | None:
    """Which kind of work-authorization question this is, or None if the label
    is in the domain but matches no category. Classification only — it reads
    no config, so the same label always lands in the same category."""
    text = label or ""
    if _NOT_WORK_AUTH.search(text):
        return "not_work_auth"
    if _EXPORT_CONTROL.search(text):
        return "export_control"
    if _FOLLOWUP.search(text):
        return "followup"
    if _NATIONALITY.search(text):
        return "nationality"
    if _STATUS_DISCLOSURE.search(text):
        return "status_disclosure"
    if _PROOF_FAMILY.search(text):
        return "proof"

    authorized = bool(_AUTHORIZED_FAMILY.search(text))
    sponsorship = bool(_SPONSORSHIP_FAMILY.search(text)) and not _SPONSORSHIP_NEGATED.search(text)
    if authorized and sponsorship:
        if _SPONSORSHIP_GOVERNS.search(text):
            authorized = False
        else:
            sponsorship = False
    if not (authorized or sponsorship):
        return None
    if options and not _offers_bare_yes_no(options):
        return "compound_option"
    if authorized and _AUTHORIZED_QUALIFIED.search(text):
        if _QUALIFIER_OFFERED_AS_ALTERNATIVE.search(text):
            return "alternation"
        return "qualified"
    return "sponsorship" if sponsorship else "plain"


_PUNCT = re.compile(r"[^\w\s]+")
_WS = re.compile(r"\s+")


class AnswersError(Exception):
    """The answer bank cannot be loaded."""


def _norm(text: str) -> str:
    """Casefolded, punctuation-free, single-spaced — the form both question
    wordings and labels are compared in."""
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'")
    return _WS.sub(" ", _PUNCT.sub(" ", text.casefold())).strip()


def _norm_option(text: str) -> str:
    """Same, but punctuation survives: option strings differ only in prose."""
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'")
    return _WS.sub(" ", text.casefold()).strip()


norm_label = _norm


@dataclass(frozen=True)
class Question:
    """One real question, however many ways boards word it."""

    id: str
    answers: tuple[str, ...] = ()
    """Literal candidates, in preference order; the first the widget offers wins."""
    wordings: tuple[str, ...] = ()
    """Normalized whole labels. An exact hit here beats every keyword."""
    keywords: tuple[str, ...] = ()
    """Normalized substrings, tried only when no question claims the whole label."""
    not_wordings: tuple[str, ...] = ()
    """Whole labels this question must not answer, even when a keyword hits."""
    computed: str = ""
    role_file: str = ""
    mode: str = "exact"
    only_when_required: bool = False
    source: str = ""
    """Where a drafted answer came from (a `B-` or `S-` id)."""


@dataclass(frozen=True)
class SalaryFloor:
    """The lowest gross salary a country's work visa allows."""

    amount: float
    currency: str
    per: str
    note: str = ""


@dataclass(frozen=True)
class CountryFacts:
    authorized: bool
    requires_sponsorship: bool
    unrestricted: bool = False
    status_label: str = ""
    followup: str = ""
    start_in_days: int | None = None
    visa_salary_floor: SalaryFloor | None = None


@dataclass(frozen=True)
class Answers:
    identity: dict[str, str]
    education: dict[str, str]
    employment: dict[str, str] | None
    questions: tuple[Question, ...]
    countries: dict[str, CountryFacts]
    nationalities: tuple[str, ...] = ()
    us_person: str = "park"
    claims: dict[str, dict] = field(default_factory=dict)
    """Normalized board option text -> the facts it states. A board's
    full-sentence work-authorization option is true of you when every fact it
    states matches the country's row."""
    demographic_answers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Topic (`DEMOGRAPHIC_TOPICS`) -> your answer's spellings, best first."""
    fact_wordings: dict[str, dict] = field(default_factory=dict)
    """Normalized label -> `{fact, yes_when}`: a yes/no question the code cannot
    classify (export control, a negated sponsorship question), judged once and
    answered from your facts on every later board."""
    cover_letter: str = "when_required"

    # Per role, set by the caller after loading (see apply_cli.build()).
    job_country: str = ""
    job_source: str = ""
    salary_figure: float | None = None
    company_answers: dict[str, str] = field(default_factory=dict)

    @property
    def employment_only_when_required(self) -> bool:
        return bool((self.employment or {}).get("only_when_required"))

    @property
    def home_country(self) -> str:
        home = self.identity.get("country", "")
        return canonical_place(home) or home

    @property
    def country(self) -> str:
        """The job's country, or the candidate's own when the posting names none."""
        return self.job_country or self.home_country

    def facts_for(self, country: str) -> CountryFacts:
        return self.countries.get(canonical_place(country) or country) or self.countries["default"]

    @property
    def authorized_now(self) -> bool:
        return self.facts_for(self.country).authorized

    @property
    def requires_sponsorship(self) -> bool:
        return self.facts_for(self.country).requires_sponsorship

    def question(self, qid: str) -> Question | None:
        return next((q for q in self.questions if q.id == qid), None)


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
    """What answered it: `bank:<id>` for an exact wording,
    `bank:<id>~<keyword>` for a keyword guess, `override:<source>` for an
    `--answers` entry. Empty where the tier is the whole story."""

    alternates: tuple[str, ...] = ()
    """Other spellings the read-back may accept for this value: a chooser with
    no option list at plan time gets the first candidate, and the board may
    settle on another one the user listed (an identity location's alternates)."""

    @property
    def parked(self) -> bool:
        return self.action == "park"

    @property
    def question_id(self) -> str:
        if not self.source.startswith("bank:"):
            return ""
        return self.source[5:].split("~", 1)[0]

    @property
    def by_keyword(self) -> bool:
        return self.source.startswith("bank:") and "~" in self.source


def _fill(value, tier: str, source: str = "") -> Resolution:
    return Resolution("fill", value=value, tier=tier, source=source)


def _skip(reason: str, tier: str = "") -> Resolution:
    return Resolution("skip", tier=tier, reason=reason)


def _park(reason: str, tier: str = "", source: str = "") -> Resolution:
    return Resolution("park", tier=tier, reason=reason, source=source)


# ---------------------------------------------------------------- loading


def _require_str(value, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AnswersError(f"{where}: expected a string, got {value!r}")
    text = str(value).strip()
    if not text:
        raise AnswersError(f"{where}: is empty")
    return text


def _require_bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise AnswersError(f"{where}: must be true or false, got {value!r}")
    return value


def _parse_block(
    data,
    key: str,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    list_keys: tuple[str, ...] = (),
) -> dict[str, str]:
    block = data.get(key)
    if not isinstance(block, dict):
        raise AnswersError(f"{key}: missing, or not a mapping")
    out = {}
    for field_key in required:
        out[field_key] = _require_str(block.get(field_key), f"{key}.{field_key}")
    for field_key in optional:
        if block.get(field_key) is not None:
            out[field_key] = _require_str(block[field_key], f"{key}.{field_key}")
    for field_key in list_keys:
        raw = block.get(field_key)
        if raw is None:
            continue
        if not isinstance(raw, list) or not raw:
            raise AnswersError(f"{key}.{field_key}: must be a non-empty list")
        if not all(isinstance(v, str) and v.strip() for v in raw):
            raise AnswersError(f"{key}.{field_key}: every entry must be a non-empty string")
        out[field_key] = tuple(v.strip() for v in raw)
    allowed = set(required) | set(optional) | set(list_keys)
    if key == "employment":
        allowed |= set(EMPLOYMENT_FLAGS)
    unknown = set(block) - allowed
    if unknown:
        raise AnswersError(f"{key}: unknown keys {sorted(unknown)}")
    return out


_COUNTRY_ROW_KEYS = (
    "authorized",
    "requires_sponsorship",
    "unrestricted",
    "status_label",
    "followup",
    "start_in_days",
    "visa_salary_floor",
)
_SALARY_FLOOR_KEYS = ("amount", "currency", "per", "note")
_SALARY_FLOOR_PER = ("year", "month")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_US_PERSON = ("park", "yes", "no")
_CLAIM_KEYS = ("authorized", "requires_sponsorship", "holds", "country")
_QID = re.compile(r"^[a-z0-9_]+$")
_QUESTION_KEYS = (
    "answer",
    "wordings",
    "keywords",
    "not",
    "mode",
    "only_when_required",
    "source",
    "from",
)


def _parse_salary_floor(raw, where: str) -> SalaryFloor | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AnswersError(f"{where}: must be {{amount, currency, per[, note]}}")
    unknown = set(raw) - set(_SALARY_FLOOR_KEYS)
    if unknown:
        raise AnswersError(f"{where}: unknown keys {sorted(unknown)}")
    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        raise AnswersError(f"{where}.amount: must be a positive number")
    currency = raw.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        raise AnswersError(f"{where}.currency: must be a three-letter ISO code, e.g. EUR")
    per = raw.get("per")
    if per not in _SALARY_FLOOR_PER:
        raise AnswersError(f"{where}.per: one of {list(_SALARY_FLOOR_PER)}")
    note = raw.get("note", "")
    if not isinstance(note, str):
        raise AnswersError(f"{where}.note: must be a string")
    return SalaryFloor(amount=amount, currency=currency, per=per, note=note.strip())


def _parse_work_authorization(data) -> tuple[dict[str, CountryFacts], tuple[str, ...], str]:
    block = data.get("work_authorization")
    if not isinstance(block, dict):
        raise AnswersError("work_authorization: missing, or not a mapping")
    unknown = set(block) - {"us_person", "nationalities", "countries"}
    if unknown:
        raise AnswersError(f"work_authorization: unknown keys {sorted(unknown)}")

    # Normalized rather than coerced: YAML reads a bare `no` as False, and a
    # silently mistyped value must not decide a legal answer.
    us_person = block.get("us_person", "park")
    if us_person not in _US_PERSON:
        raise AnswersError(
            f"work_authorization.us_person: {us_person!r} is not one of "
            f"{list(_US_PERSON)} — quote it, since YAML reads a bare yes/no as a boolean"
        )

    nationalities = block.get("nationalities") or []
    if not isinstance(nationalities, list) or not all(
        isinstance(n, str) and n.strip() for n in nationalities
    ):
        raise AnswersError("work_authorization.nationalities: must be a list of countries")

    rows = block.get("countries")
    if not isinstance(rows, dict) or "default" not in rows:
        raise AnswersError(
            "work_authorization.countries: needs a `default` row, for every "
            "country you have not listed"
        )
    countries: dict[str, CountryFacts] = {}
    for name, row in rows.items():
        where = f"work_authorization.countries.{name}"
        if not isinstance(row, dict):
            raise AnswersError(f"{where}: not a mapping")
        unknown = set(row) - set(_COUNTRY_ROW_KEYS)
        if unknown:
            raise AnswersError(f"{where}: unknown keys {sorted(unknown)}")
        authorized = _require_bool(row.get("authorized"), f"{where}.authorized")
        requires = _require_bool(row.get("requires_sponsorship"), f"{where}.requires_sponsorship")
        unrestricted = _require_bool(row.get("unrestricted", False), f"{where}.unrestricted")
        if unrestricted and (not authorized or requires):
            raise AnswersError(
                f"{where}: unrestricted means authorized with no sponsorship ever; "
                f"it contradicts authorized={authorized}, requires_sponsorship={requires}"
            )
        start = row.get("start_in_days")
        if start is not None and (
            isinstance(start, bool) or not isinstance(start, int) or start < 0
        ):
            raise AnswersError(f"{where}.start_in_days: must be a whole number of days")
        for text_key in ("status_label", "followup"):
            if not isinstance(row.get(text_key, ""), str):
                raise AnswersError(f"{where}.{text_key}: must be a string")
        floor = _parse_salary_floor(row.get("visa_salary_floor"), f"{where}.visa_salary_floor")
        key = canonical_place(str(name))
        if not key:
            raise AnswersError(f"{where}: not a country or region this pipeline knows")
        if key in countries:
            raise AnswersError(f"{where}: another row already names {key}")
        countries[key] = CountryFacts(
            authorized=authorized,
            requires_sponsorship=requires,
            unrestricted=unrestricted,
            status_label=str(row.get("status_label", "")).strip(),
            followup=str(row.get("followup", "")).strip(),
            start_in_days=start,
            visa_salary_floor=floor,
        )
    return countries, tuple(n.strip() for n in nationalities), us_person


def _parse_claims(data) -> dict[str, dict]:
    raw = data.get("claims") or {}
    if not isinstance(raw, dict):
        raise AnswersError("claims: must be a mapping of option text to facts")
    out: dict[str, dict] = {}
    for option, facts in raw.items():
        where = f"claims[{option!r}]"
        key = _norm_option(str(option))
        if key in _BARE_OPTIONS:
            raise AnswersError(f"{where}: too generic to carry a claim of its own")
        if not isinstance(facts, dict) or not facts:
            raise AnswersError(f"{where}: must map to the facts it states")
        unknown = set(facts) - set(_CLAIM_KEYS)
        if unknown:
            raise AnswersError(f"{where}: unknown keys {sorted(unknown)}")
        claim = {}
        for k in ("authorized", "requires_sponsorship", "holds"):
            if k in facts:
                claim[k] = _require_bool(facts[k], f"{where}.{k}")
        if "country" in facts:
            place = canonical_place(_require_str(facts["country"], f"{where}.country"))
            if not place:
                raise AnswersError(f"{where}.country: not a country this pipeline knows")
            claim["country"] = place
        if not set(claim) - {"country"}:
            raise AnswersError(f"{where}: states no fact")
        out[key] = claim
    return out


def _parse_demographics(data) -> dict[str, tuple[str, ...]]:
    block = data.get("demographics")
    if block is None:
        return {}
    if not isinstance(block, dict) or set(block) - {"answers"}:
        raise AnswersError("demographics: only `answers` is allowed")
    raw = block.get("answers") or {}
    if not isinstance(raw, dict):
        raise AnswersError("demographics.answers: must map a topic to your answer")
    out = {}
    for topic, value in raw.items():
        where = f"demographics.answers.{topic}"
        if topic not in DEMOGRAPHIC_TOPICS:
            raise AnswersError(f"{where}: a topic is one of {list(DEMOGRAPHIC_TOPICS)}")
        values = value if isinstance(value, list) else [value]
        if not values:
            raise AnswersError(f"{where}: must not be empty")
        out[topic] = tuple(_require_str(v, where) for v in values)
    return out


# The facts a judged wording may be answered from. `us_person` is the bank's
# own; the rest are the question's country's row.
FACT_WORDING_FACTS = ("us_person", "authorized", "requires_sponsorship", "unrestricted")


def _parse_fact_wordings(data) -> dict[str, dict]:
    raw = data.get("fact_wordings") or {}
    if not isinstance(raw, dict):
        raise AnswersError("fact_wordings: must map a label to {fact, yes_when}")
    out: dict[str, dict] = {}
    for label, spec in raw.items():
        where = f"fact_wordings[{label!r}]"
        key = _norm(str(label))
        if not key:
            raise AnswersError(f"{where}: the label normalizes to nothing")
        if not isinstance(spec, dict) or set(spec) != {"fact", "yes_when"}:
            raise AnswersError(f"{where}: must be {{fact, yes_when}}")
        if spec["fact"] not in FACT_WORDING_FACTS:
            raise AnswersError(f"{where}.fact: one of {list(FACT_WORDING_FACTS)}")
        _require_bool(spec["yes_when"], f"{where}.yes_when")
        if not in_work_authorization_domain(str(label)):
            raise AnswersError(
                f"{where}: not a work-authorization or export-control question; "
                "an ordinary question goes under questions:"
            )
        if key in out:
            raise AnswersError(f"{where}: another label normalizes to the same wording")
        out[key] = {"fact": spec["fact"], "yes_when": spec["yes_when"]}
    return out


def _fact_candidates(path: str, identity, education, employment, where: str) -> tuple[str, ...]:
    blocks = {"identity": identity, "education": education, "employment": employment or {}}
    head, _, key = path.partition(".")
    block = blocks.get(head)
    if block is None or key not in block:
        raise AnswersError(f"{where}: fact {path!r} is not set")
    value = block[key]
    alternates = block.get(f"{key}_alternates") or ()
    return (str(value), *(a for a in alternates if a != value))


def _normed_list(raw, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AnswersError(f"{where}: must be a list")
    out = tuple(_norm(_require_str(v, where)) for v in raw)
    if any(not v for v in out):
        raise AnswersError(f"{where}: an entry normalizes to nothing")
    return out


def _parse_questions(data, identity, education, employment) -> tuple[Question, ...]:
    raw = data.get("questions") or {}
    if not isinstance(raw, dict):
        raise AnswersError("questions: must be a mapping of id to question")
    questions: list[Question] = []
    for qid, spec in raw.items():
        where = f"questions.{qid}"
        if not isinstance(qid, str) or not _QID.match(qid):
            raise AnswersError(f"{where}: an id is lowercase letters, digits and _")
        if not isinstance(spec, dict):
            raise AnswersError(f"{where}: not a mapping")
        unknown = set(spec) - set(_QUESTION_KEYS)
        if unknown:
            raise AnswersError(f"{where}: unknown keys {sorted(unknown)}")

        answers: tuple[str, ...] = ()
        computed = role_file = ""
        answer = spec.get("answer")
        if isinstance(answer, dict):
            if set(answer) == {"fact"}:
                answers = _fact_candidates(
                    _require_str(answer["fact"], where), identity, education, employment, where
                )
            elif set(answer) == {"computed"}:
                computed = _require_str(answer["computed"], where)
                if computed not in COMPUTED:
                    raise AnswersError(f"{where}: computed must be one of {list(COMPUTED)}")
            elif set(answer) == {"computed", "fallback"}:
                computed = _require_str(answer["computed"], where)
                if computed not in COMPUTED:
                    raise AnswersError(f"{where}: computed must be one of {list(COMPUTED)}")
                fallback = answer["fallback"]
                fallback = fallback if isinstance(fallback, list) else [fallback]
                answers = tuple(_require_str(a, f"{where}.fallback") for a in fallback)
            elif set(answer) == {"role_file"}:
                role_file = _require_str(answer["role_file"], where)
            else:
                raise AnswersError(
                    f"{where}.answer: a mapping is one of {{fact}}, {{computed}}, "
                    f"{{computed, fallback}}, {{role_file}}"
                )
        elif answer is not None:
            candidates = answer if isinstance(answer, list) else [answer]
            if not candidates:
                raise AnswersError(f"{where}.answer: must not be empty")
            answers = tuple(_require_str(a, f"{where}.answer") for a in candidates)
        else:
            raise AnswersError(f"{where}: needs an answer")

        wordings = _normed_list(spec.get("wordings"), f"{where}.wordings")
        keywords = _normed_list(spec.get("keywords"), f"{where}.keywords")
        not_wordings = _normed_list(spec.get("not"), f"{where}.not")
        if not wordings and not keywords:
            raise AnswersError(f"{where}: needs wordings or keywords to be found by")
        for original, keyword in zip(spec.get("keywords") or [], keywords):
            if len(keyword) < MIN_KEYWORD_LENGTH:
                raise AnswersError(
                    f"{where}.keywords: {original!r} normalizes to {keyword!r}, under "
                    f"{MIN_KEYWORD_LENGTH} characters — put the whole label in "
                    f"`wordings:` instead"
                )
        for text in wordings + keywords:
            if WORK_AUTHORIZATION_DOMAIN.search(text) and not _NOT_WORK_AUTH.search(text):
                raise AnswersError(
                    f"{where}: {text!r} is a work-authorization question. Those are "
                    "answered from work_authorization, per country, never from a question"
                )
        mode = spec.get("mode", "exact")
        if mode not in ("exact", "contains"):
            raise AnswersError(f"{where}.mode: must be 'exact' or 'contains', got {mode!r}")
        only_when_required = spec.get("only_when_required", False)
        _require_bool(only_when_required, f"{where}.only_when_required")
        questions.append(
            Question(
                id=qid,
                answers=answers,
                wordings=wordings,
                keywords=keywords,
                not_wordings=not_wordings,
                computed=computed,
                role_file=role_file,
                mode=mode,
                only_when_required=only_when_required,
                source=str(spec.get("source") or "").strip(),
            )
        )

    # A wording claimed by two questions has no single answer.
    owner: dict[str, str] = {}
    for q in questions:
        for w in q.wordings:
            if w in owner and owner[w] != q.id:
                raise AnswersError(
                    f"questions.{owner[w]} and questions.{q.id} both claim the wording {w!r}"
                )
            owner[w] = q.id
    # Keywords are substring tests in file order, so an overlap is a question
    # silently shadowing another.
    for i, q in enumerate(questions):
        for other in questions[i + 1 :]:
            for a in q.keywords:
                for b in other.keywords:
                    if a in b or b in a:
                        raise AnswersError(
                            f"questions.{q.id} keyword {a!r} overlaps questions."
                            f"{other.id} keyword {b!r}: one would shadow the other"
                        )
    return tuple(questions)


def load_answers(path: Path | None = None) -> Answers:
    """Load and validate the answer bank. Fails loud on anything ambiguous: a
    wrong answer here is sent under the user's name."""
    p = Path(path) if path is not None else paths.ANSWER_BANK
    if not p.exists():
        raise AnswersError(f"{p} missing. Copy {paths.example(EXAMPLE_NAME)} and fill it in.")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AnswersError(f"Malformed YAML in {p}: {exc}") from exc
    return answers_from(data, where=str(p))


def answers_from(data, where: str = "answer bank") -> Answers:
    if not isinstance(data, dict):
        raise AnswersError(f"{where}: top level must be a mapping")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise AnswersError(f"{where}: schema_version must be {SCHEMA_VERSION}")
    unknown_top = set(data) - set(TOP_LEVEL_KEYS)
    if unknown_top:
        raise AnswersError(
            f"{where}: unknown top-level keys {sorted(unknown_top)} "
            f"(expected {sorted(TOP_LEVEL_KEYS)})"
        )

    identity = _parse_block(data, "identity", IDENTITY_KEYS, list_keys=IDENTITY_LIST_KEYS)
    education = _parse_block(
        data, "education", EDUCATION_KEYS, EDUCATION_OPTIONAL_KEYS, EDUCATION_LIST_KEYS
    )
    employment = None
    if data.get("employment") is not None:
        employment = _parse_block(data, "employment", EMPLOYMENT_KEYS)
        for flag in EMPLOYMENT_FLAGS:
            employment[flag] = bool(data["employment"].get(flag))

    countries, nationalities, us_person = _parse_work_authorization(data)

    cover_letter = data.get("cover_letter", "when_required")
    if cover_letter not in COVER_LETTER_POLICIES:
        raise AnswersError(
            f"cover_letter: {cover_letter!r} is not one of {list(COVER_LETTER_POLICIES)}"
        )

    return Answers(
        identity=identity,
        education=education,
        employment=employment,
        questions=_parse_questions(data, identity, education, employment),
        countries=countries,
        nationalities=nationalities,
        us_person=us_person,
        claims=_parse_claims(data),
        demographic_answers=_parse_demographics(data),
        fact_wordings=_parse_fact_wordings(data),
        cover_letter=cover_letter,
    )


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
        picked = _resolve_choice(field, candidates, "A", f"identity.{key}")
        return replace(picked, alternates=candidates) if picked.action == "fill" else picked
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


def _resolve_nationality(field: MergedField, answers: Answers) -> Resolution:
    """Nationality questions, split by shape so one fact never answers three
    different questions: a second citizenship, a region/country yes-no, and
    the direct ask."""
    nationalities = answers.nationalities
    label = field.label or ""
    if not nationalities:
        return _park("nationality: work_authorization.nationalities is not set", "B0")

    if _SECOND_NATIONALITY.search(label):
        second = nationalities[1] if len(nationalities) > 1 else ""
        if _offers_bare_yes_no(tuple(o.label for o in field.options)):
            answered = _yes_no(field, bool(second))
            if answered is not None:
                return answered
        candidates = (second,) if second else _NONE_CANDIDATES
        picked = _pick_option(field, candidates)
        if picked is None:
            return _park(
                f"nationality: second-citizenship question offers no option "
                f"matching {(second or 'None')!r}",
                "B0",
            )
        return _fill(picked, "B0")

    options = tuple(o.label for o in field.options)
    region = _nationality_region_match(_norm_option(label), nationalities)
    if region is not None and (not options or _offers_bare_yes_no(options)):
        answered = _yes_no(field, region)
        if answered is not None:
            return answered
    if options and _offers_bare_yes_no(options):
        return _park(
            "nationality: this yes/no question names a country or region not in "
            "the lookup — extend it rather than guess",
            "B0",
        )

    picked = _pick_option(field, nationalities)
    if picked is None:
        return _park(f"nationality: {list(nationalities)} not among the offered options", "B0")
    return _fill(picked, "B0")


def _question_countries(label: str, answers: Answers) -> list[str]:
    named = places_named(label)
    return named or [answers.country]


def _one_value(label: str, answers: Answers, fact) -> tuple[bool | None, str]:
    """The fact across every country the label names: any of them for "the US
    or Canada", all of them otherwise ("the US and the UK")."""
    countries = _question_countries(label, answers)
    values = [fact(answers.facts_for(c)) for c in countries]
    if len(countries) > 1 and _either_place(label):
        return any(values), ""
    return all(values), ""


def _true_options(
    field: MergedField, answers: Answers, country: str, asked: str | None
) -> list[str]:
    """Options this board offers that are known to be true of you in `country`.

    An option is true when the bank has a claim for its exact text and every
    fact the claim states matches the country's row. `asked` names the fact
    the question is about: an option that says nothing about it cannot answer
    it, however true it is."""
    facts = answers.facts_for(country)
    out = []
    for option in field.options:
        claim = answers.claims.get(_norm_option(option.label))
        if not claim:
            continue
        if claim.get("country") and canonical_place(claim["country"]) != canonical_place(country):
            continue
        if asked and asked not in claim and not claim.get("holds"):
            continue
        if any(
            getattr(facts, k) != v
            for k, v in claim.items()
            if k in ("authorized", "requires_sponsorship")
        ):
            continue
        if claim.get("holds") is False:
            continue
        out.append(option.label)
    return out


def _resolve_by_claims(
    field: MergedField, answers: Answers, asked: str | None, why: str
) -> Resolution:
    countries = _question_countries(field.label, answers)
    if len(countries) != 1:
        return _park(f"work authorization: names {countries}; pick by hand", "B0")
    true = _true_options(field, answers, countries[0], asked)
    if true:
        return _fill(tuple(true) if field.multi else true[0], "B0", source=f"claim:{countries[0]}")
    offered = [o.label for o in field.options]
    return _park(
        f"work authorization: {why} No offered option is known to be true for "
        f"{countries[0]}; add the true one under claims (offered: {offered})",
        "B0",
    )


def in_work_authorization_domain(label: str) -> bool:
    """Whether a label goes to the work-authorization resolver."""
    return bool(
        WORK_AUTHORIZATION_DOMAIN.search(label or "") and not _NOT_WORK_AUTH.search(label or "")
    )


def resolve_fact_wording(field: MergedField, answers: Answers, judged: dict) -> Resolution:
    """A wording judged once: Yes exactly when the fact it names equals
    `yes_when`, so the answer follows your facts if they change."""
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
        value = answers.us_person == "yes"
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


# A yes/no family's label is only a suggestion: the fact it reads, Yes when true.
# Confirmed in /apply, the wording becomes an exact `fact_wordings` entry.
SUGGESTED_FACT = {
    "plain": "authorized",
    "proof": "authorized",
    "qualified": "unrestricted",
    "sponsorship": "requires_sponsorship",
}


def _resolve_work_authorization(field: MergedField, answers: Answers) -> Resolution:
    label = field.label or ""
    judged = answers.fact_wordings.get(_norm(label))
    options = tuple(o.label for o in field.options)
    # A board whose options are sentences is answered by claims, whatever the wording.
    if judged is not None and (not options or _offers_bare_yes_no(options)):
        return resolve_fact_wording(field, answers, judged)
    category = classify_work_authorization(label, options)
    countries = _question_countries(label, answers)
    if UNKNOWN_COUNTRY in countries and category not in (
        "nationality",
        "export_control",
        "not_work_auth",
    ):
        return _park(
            "work authorization: the job's country could not be read from "
            "its location, and this question depends on it",
            "B0",
        )
    facts = answers.facts_for(countries[0])

    if category == "export_control":
        if not _US_PERSON_QUESTION.search(label):
            return _park(
                "export control: yours to answer, every time", "B0", source="export_control"
            )
        if answers.us_person == "park":
            return _park(
                'export control: asks whether you are a "U.S. person", a legal '
                "declaration distinct from work authorization. Set "
                "work_authorization.us_person to answer it",
                "B0",
                source="export_control",
            )
        answered = _yes_no(field, answers.us_person == "yes")
        if answered is not None:
            return Resolution("fill", answered.value, "B0", source="export_control")
        return _park("export control: no bare Yes/No offered", "B0", source="export_control")
    if category == "nationality":
        return _resolve_nationality(field, answers)
    if category == "followup":
        if facts.followup and not field.options and len(countries) == 1:
            return _fill(facts.followup, "B0")
        return _park(
            f"work authorization: a follow-up asking for detail. Set "
            f"countries.{countries[0]}.followup to answer it",
            "B0",
        )
    if category == "status_disclosure":
        if field.options:
            return _resolve_by_claims(field, answers, None, "asks which status you hold.")
        if facts.status_label and len(countries) == 1:
            return _fill(facts.status_label, "B0")
        return _park(
            f"work authorization: asks which status you hold. Set "
            f"countries.{countries[0]}.status_label",
            "B0",
        )
    if category == "compound_option":
        asked = (
            "requires_sponsorship"
            if _SPONSORSHIP_FAMILY.search(label) and not _SPONSORSHIP_NEGATED.search(label)
            else "authorized"
        )
        return _resolve_by_claims(field, answers, asked, "every option carries its own claim.")
    if category == "alternation":
        return _park(
            "work authorization: the scope qualifier is offered as an alternative "
            '("permanent or temporary"), so it does not narrow the question',
            "B0",
        )
    if category is None:
        return _park("work authorization: label matches no answerable family", "B0")

    if category == "qualified":
        value, why = _one_value(label, answers, lambda f: f.authorized and f.unrestricted)
        asked = "authorized"
    elif category == "sponsorship":
        value, why = _one_value(label, answers, lambda f: f.requires_sponsorship)
        asked = "requires_sponsorship"
    else:  # plain, proof
        value, why = _one_value(label, answers, lambda f: f.authorized)
        asked = "authorized"
    if value is None:
        return _park(f"work authorization: {why}", "B0")

    answered = _yes_no(field, value)
    if answered is not None:
        fact = SUGGESTED_FACT[category]
        return _park(
            f"work authorization: new wording, confirm once. Suggested: Yes when {fact} "
            f"is true (answers {answered.value!r} here)",
            "B0",
            source=f"suggest:{fact}",
        )
    # No bare Yes/No for this polarity — the answer is split across sentences.
    return _resolve_by_claims(field, answers, asked, "the board offers no bare Yes/No.")


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
            f"salary: no figure for a role in {country} (verticals.yaml salary_expectation)",
            "B",
            source=source,
        )
    figure = round(answers.salary_figure)
    candidates = (str(figure), f"${figure:,}")
    if field.options or field.kind == "react_select":
        return _resolve_choice(field, candidates, "B", question.id, source=source)
    return _fill(str(figure), "B", source=source)


def find_question(label: str, answers: Answers) -> tuple[Question, str] | None:
    """The question a label asks, and the source tag naming how it was found:
    `bank:<id>` for an exact wording, `bank:<id>~<keyword>` for a keyword."""
    norm = _norm(label)
    if not norm:
        return None
    for q in answers.questions:
        if norm in q.wordings:
            return q, f"bank:{q.id}"
    for q in answers.questions:
        if norm in q.not_wordings:
            continue
        for keyword in q.keywords:
            if keyword in norm:
                return q, f"bank:{q.id}~{keyword}"
    return None


_STOPWORDS = frozenset(
    "a an and any are as at be by can do does did for from have has how i if in is it "
    "of on or our please that the this to us we what when where which who why will with "
    "would you your yours".split()
)


def _words(text: str) -> set[str]:
    return {w for w in _norm(text).split() if w not in _STOPWORDS and len(w) > 2}


def similar_questions(label: str, answers: Answers, n: int = 3) -> tuple[str, ...]:
    """The bank questions whose wordings and keywords share the most words with
    a label, best first — the ids a session should choose from before it
    invents a new one."""
    want = _words(label)
    if not want:
        return ()
    scored = []
    for q in answers.questions:
        have = set().union(*(_words(t) for t in (*q.wordings, *q.keywords, q.id.replace("_", " "))))
        shared = len(want & have)
        if shared:
            scored.append((shared / len(want | have), q.id))
    return tuple(qid for _, qid in sorted(scored, reverse=True)[:n])


def _resolve_question(field: MergedField, answers: Answers) -> Resolution | None:
    found = find_question(field.label, answers)
    if found is None:
        return None
    question, source = found
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
    if field.options or field.kind == "react_select":
        return _resolve_choice(
            field, question.answers, "B", question.id, mode=question.mode, source=source
        )
    return _fill(question.answers[0], "B", source=source)


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

    label = field.label or ""
    if in_work_authorization_domain(label):
        resolution = _resolve_work_authorization(field, answers)
        # Blank is never a false claim, and an optional free-text visa question
        # is not worth parking a role over.
        if resolution.parked and not field.required:
            return Resolution("skip", tier="B0", reason=resolution.reason, source=resolution.source)
        return resolution

    question = _resolve_question(field, answers)
    if question is not None:
        return question

    # Tier C: nothing in the bank asks this. The /apply session maps it to a
    # question, and the run saves that mapping for next time.
    return (
        _park("no question in the bank asks this", "C")
        if field.required
        else _skip("optional, not in the bank", "C")
    )
