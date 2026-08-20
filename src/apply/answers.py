"""Resolve a rendered application field to the value that goes into it.

Deterministic: config lookup, structural rules for the blocks Greenhouse owns,
and keyword rules for the employer-authored ones. Nothing here judges, drafts or
guesses — a field this module cannot resolve comes back parked, and a parked
field stops the submission.

Resolution order, keyed on the merged field's section and id before its prose:

    A   identity / education / employment  -> application_answers.yaml blocks
    A   kind == "date"                     -> today, computed
    A2  eeoc / demographic                 -> opt out, structurally
    B0  work authorization                 -> work_authorization.status
    PARSED money question, parsed_salary set -> clean.parquet compensation
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
from datetime import date
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
#: Optional, list-valued. The same location as a board's own taxonomy words it —
#: one ATS abbreviates state and country where another spells both out, and an
#: exact-match chooser takes neither for the other. Tried in order after
#: `location`.
IDENTITY_LIST_KEYS = ("location_alternates",)
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
# Flags on the employment block, parsed as booleans rather than strings.
EMPLOYMENT_FLAGS = ("current_role", "only_when_required")

TOP_LEVEL_KEYS = (
    "schema_version", "identity", "work_authorization", "education",
    "employment", "rules",
    # Not an answer: which browser drives the form. Owned and validated by
    # src/apply/browser.py, listed here only so the top-level check above does
    # not reject a key it does not parse.
    "browser",
)

# A `match:` keyword this short matches almost any label. Punctuation is
# stripped before matching, so `C++` normalizes to the single character `c`
# and answers "What are your salary expectations?" from a rule about C++.
# Validation only rejected keywords that normalized to *empty*.
MIN_KEYWORD_LENGTH = 3

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
    "location": "location",     # Lever's raw DOM name for the same concept
    "country": "country",
    "_systemfield_email": "email",       # Ashby's raw systemfield id
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

# EEOC opt-out, per DOM id, matched against the options the form actually
# renders. Every board observed offers exactly these strings. The eeo[...]
# keys are Lever's raw DOM names for the same four concepts — Lever's scan
# does not alias id away from name (fill.py needs name intact as the real
# selector), so both spellings have to resolve.
_EEOC_OPT_OUT = {
    "gender": ("Decline To Self Identify",),
    "eeo[gender]": ("Decline To Self Identify",),
    "race": ("Decline To Self Identify",),
    "eeo[race]": ("Decline To Self Identify",),
    "veteran_status": ("I don't wish to answer",),
    "eeo[veteran]": ("I don't wish to answer",),
    "disability_status": ("I do not want to answer",),
    "eeo[disability]": ("I do not want to answer",),
}
# The self-identification form attests who filled it in, and both fields become
# required the moment the disability question is answered at all — the board
# says so in as many words: "Name and date are only required if you filled out
# Disability status." Neither is an opt-out question, so `_resolve_eeoc`'s
# option matching has nothing to match and both were skipped, which left the
# browser's own constraint validation refusing the form: the submit click
# fired no request at all and the role was recorded as applied anyway.
#
# Both values are already known — the configured name and the clock. Nothing
# here is a judgment call.
#: Every date this module writes. Not a formatting preference: verified live
#: against a real board's picker, where ISO (`YYYY-MM-DD`) silently misparsed
#: by a day and `DD/MM/YYYY` silently swapped month and day — both with no
#: error shown. See `_resolve_date`. No ATS declares a format: Ashby's GraphQL
#: gives the type (`Date`) and not the shape, Greenhouse's question API the
#: same, and Lever has no API at all — its only signal is the DOM placeholder,
#: which reads MM/DD/YYYY on every board observed.
DATE_FORMAT = "%m/%d/%Y"

_EEOC_SIGNATURE_IDS = frozenset({
    "eeo[disabilitySignature]",
    "disability_signature",
})
_EEOC_SIGNATURE_DATE_IDS = frozenset({
    "eeo[disabilitySignatureDate]",
    "disability_signature_date",
})

# hispanic_ethnicity is DOM-only: no API question, so nothing here has an option
# list to match against and it parks. That is not a policy of leaving it blank —
# opened in a browser it offers Yes / No / Decline To Self Identify, so the
# generic opt-out below answers it. fill.py re-resolves it once it can read the
# widget (§4). Nothing changes for the static path: no options, still parks.

# Tried in order after the per-id string above, and for demographic questions
# whose decline option carries no flag.
_OPT_OUT_FALLBACKS = (
    "I don't wish to answer",
    "I do not wish to answer",
    "I do not want to answer",
    "Decline To Self Identify",
    "Decline to self identify",
    "Decline to self-identify",
    "I prefer not to answer",
    "Prefer not to say",
    "I decline to self-identify for protected veteran status",
)

# Anything salary/compensation shaped — same keyword set the `salary`/
# `compensation` template rule in application_answers.yaml matches, and the one
# `/apply` Step 4b scans "fields"/"unmapped"/"draftable" for. Kept here as a
# domain check (rather than only a `rules[]` keyword) so `_resolve_parsed_salary`
# can recognize a money question even when the user's own `rules:` list doesn't
# happen to cover it verbatim.
_MONEY_DOMAIN = re.compile(
    r"salary|compensation|desired\s+rate\s+of\s+pay|pay\s+expectation",
    re.IGNORECASE,
)

# Anything work-authorization shaped. A rules[] keyword that lands in here is a
# load error; a question label that lands in here is answered by exactly one of
# the two families below, or parked.
WORK_AUTHORIZATION_DOMAIN = re.compile(
    r"sponsor|visa|work\s+authori[sz]|authou?ri[sz]\w*\s+to\s+work|citizenship|right\s+to\s+work"
    # Widened from the 237-board harvest, where a whole family of ordinary
    # phrasings sat outside the domain entirely and fell through to tier C:
    # "Are you currently eligible to work in the United States?", "Work
    # Eligibility Status", "Work Status", "What is your nationality?".
    r"|eligib\w*\s+to\s+(?:work|live)|work\s+eligib|employment\s+eligib"
    r"|work\s+status|nationalit|\bcitizen\b|optional\s+practical\s+training"
    # "Please confirm you are authorized a to work in USA or Canada" — a typo on
    # a live board that the strict "authorized to work" form missed.
    r"|authou?ri[sz]\w*\s+\w{0,3}\s*to\s+work"
    # "Country of origin" — a nationality question in different clothes, seen
    # live on ALX Africa's Greenhouse board. Same reasoning as `nationalit`:
    # a country is not a visa status, so it must park rather than fall to
    # tier C, where the /apply session could mistake it for an ordinary one.
    r"|country\s+of\s+origin",
    re.IGNORECASE,
)
# Caught by the widened domain above but NOT work authorization. Checked FIRST,
# because these must keep falling through to tier C where the /apply session
# answers them as ordinary questions — pulling them into the work-auth resolver
# parks them at B0, and a B0 park makes the whole role manual-apply.
#
# Positive shape match, not keyword-absence: the relocation-cities checkbox
# names "visa sponsorship" in its own preamble ("we offer relocation support
# including visa sponsorship, housing assistance and more") and would stay
# in-domain however the domain regex were written.
_NOT_WORK_AUTH = re.compile(
    r"relocation\s+support"                       # relocation-city checkbox
    r"|at\s+least\s+18\s+years"                   # age eligibility
    r"|security\s+clearance|clearance\s+eligib"    # clearance, not work auth
    r"|notetaker|ai\s+interview\s+tool"            # interview-process consent
    r"|privacy\s+notice|privacy\s+policy"          # policy acknowledgement
    r"|hereby\s+certify|knowingly\s+withheld",     # application certification
    re.IGNORECASE,
)
_AUTHORIZED_FAMILY = re.compile(
    r"authou?ri[sz]\w*\s+to\s+work|legally\s+authou?ri[sz]"
    # Same question, three more spellings: "Do you have the legal right to
    # work in...", "Are you legally eligible to work in the US?", "Do you have
    # valid U.S. work authorization?".
    r"|right\s+to\s+work|eligib\w*\s+to\s+(?:work|live)"
    r"|(?:have|hold)\s+(?:\S+\s+){0,3}work\s+authori[sz]"
    r"|authou?ri[sz]\w*\s+\w{0,3}\s*to\s+work",
    re.IGNORECASE,
)
# "Can you provide proof of your authorization?" is answerable by anyone who is
# authorized at all — it asks about documentation, not about scope.
_PROOF_FAMILY = re.compile(
    r"(?:proof|evidence|documentation)\s+of\s+(?:\w+\s+){0,3}"
    r"(?:authoriz|eligib|right\s+to\s+work)"
    r"|able\s+to\s+provide\s+(?:\w+\s+){0,3}(?:proof|documentation)",
    re.IGNORECASE,
)
_SPONSORSHIP_FAMILY = re.compile(
    r"(?:requir|need)\w*[^?]*sponsor|sponsor\w*[^?]*(?:requir|need)"
    # Two shapes that ask the same thing without the word "sponsor": "Do you
    # need a work visa?", "Do you require work authorization?".
    r"|(?:requir|need)\w*\s+(?:\S+\s+){0,3}(?:work\s+)?visa"
    r"|(?:requir|need)\w*\s+(?:\S+\s+){0,3}work\s+authori[sz]",
    re.IGNORECASE,
)
# "Are you able to work without sponsorship?" inverts the answer. Never guess it.
_SPONSORSHIP_NEGATED = re.compile(
    r"without\s+(?:\w+\s+){0,2}sponsor|not\s+(?:\w+\s+){0,2}requir\w*[^?]*sponsor",
    re.IGNORECASE,
)
# When a label reads as both families, which one is actually being asked. These
# all open with the sponsorship verb and mention authorization only as the thing
# the sponsorship would maintain: "Will you require sponsorship ... to maintain
# authorization to work in the United States?". Four observed shapes.
_SPONSORSHIP_GOVERNS = re.compile(
    r"^\s*(?:will|do|would)\s+you\s+(?:\S+\s+){0,8}"
    r"(?:requir|need)\w*\s+(?:\S+\s+){0,4}sponsor"
    r"|(?:requir|need)\w*\s+(?:\w+\s+){0,3}sponsor\w*\s+(?:\w+\s+){0,4}"
    r"(?:to|for)\s+(?:maintain|retain|extend|continue|obtain|remain|commence)",
    re.IGNORECASE,
)
# The same never-guess policy, for the authorization family. `authorized_now`
# answers exactly one question: may you work in the US at all, today. Any
# scope qualifier — permanence, employer-independence, freedom from
# sponsorship — asks something else, and answering it from `authorized_now`
# states a falsehood for a time_limited status. The shapes: "Are you
# permanently authorized to work for any employer in the United States?" and
# "I am authorized to work without sponsorship or restrictions for any
# employer in the U.S."
_AUTHORIZED_QUALIFIED = re.compile(
    r"permanent\w*"
    r"|ongoing"
    r"|indefinite\w*"
    r"|unrestricted"
    # "...without the need for current or future employer sponsorship?" puts
    # seven words between the two, so a narrow gap read it as the plain question.
    r"|without\s+(?:\S+\s+){0,8}(?:sponsor|restrict)"
    r"|now\s+or\s+in\s+the\s+future"
    r"|no\s+(?:\w+\s+){0,2}restrict",
    re.IGNORECASE,
)
# NB: "for any employer" is deliberately NOT a qualifier. It reads like one,
# but it asks about employer-tying, and OPT is not employer-tied the way an
# H-1B is — the honest answer is Yes. Questions that pair it with a real
# qualifier ("Are you *permanently* authorized to work for any employer?")
# still park on that qualifier, which is the correct outcome.
# ...except where the qualifier is offered as one *alternative* rather than as
# the requirement. "Are you authorized to work in the US on a permanent or
# temporary basis?" trips `_AUTHORIZED_QUALIFIED` on "permanent", but the
# truthful answer for a time-limited status is Yes — the question already
# allows for temporary. Answering it from `scope_qualified_answer` would state
# the user cannot work here at all.
#
# So this stays a park no matter how `scope_qualified_answer` is set: the
# setting says what the answer is when the scope really is narrowed, and here
# it is not. Deliberately narrow — three observed shapes, not a general
# attempt to parse alternation.
_QUALIFIER_OFFERED_AS_ALTERNATIVE = re.compile(
    r"permanent\w*\s+or\s+\w+"
    r"|\w+\s+or\s+permanent\w*"
    r"|with\s+or\s+without",
    re.IGNORECASE,
)

# What to answer when a question narrows the scope of "authorized to work"
# beyond what `status` alone settles. Not derived from `status`, because it is
# not derivable: it is a statement about the user's own circumstances that only
# they can make. `park` is the default and hands the question back to them.
SCOPE_QUALIFIED_ANSWERS = ("park", "yes", "no")
# Whether you are a "U.S. person" as ITAR/EAR define it (citizen, permanent
# resident, refugee or asylee) -- NOT the same as work authorization, and a
# wrong answer has consequences past this application. `park` (the default)
# always hands the question back to the user, same as scope_qualified_answer's
# default; only an explicit "yes"/"no" here lets it auto-fill.
US_PERSON_ANSWERS = ("park", "yes", "no")
WORK_AUTHORIZATION_KEYS = ("status", "scope_qualified_answer", "status_label",
                           "status_option_candidates", "nationality",
                           "second_nationality", "sponsorship_followup_text",
                           "us_person_answer")
# The authorization question is country-scoped; the sponsorship one is not, and
# names a country on only 2 of the 5 captured boards. The queue only ever holds
# roles that passed discovery's US location allowlist.
_NAMES_THE_US = re.compile(
    r"united\s+states|u\.s\.a?\.|\bu\.s\b|\busa\b|america", re.IGNORECASE
)
_NAMES_US_ABBREV = re.compile(r"\bUS\b")  # case-sensitive: "us" is a pronoun
# A country that is NOT the US, named outright. This is the only case that has
# to park: an unnamed country is the *job's* country, and the queue only ever
# holds roles that passed discovery's US location allowlist, so "authorized to
# work in the country where this role is located" is a US question. Requiring
# an explicit "United States" parked 14 of 21 required work-auth questions on
# live Ashby boards for no gain.
#
# Checked only after the US test, so "the US or Canada" resolves rather than
# parking. Derived from the harvested corpus, where exactly one label of 77
# names a foreign country ("...authorised to work in the UK without employer
# sponsorship?"); the rest of this list is the ordinary set a US-based search
# can expect to meet. A country not listed here reads as unnamed and is
# answered as the US — the same assumption the location allowlist already
# makes.
_NAMES_OTHER_COUNTRY = re.compile(
    r"\bcanada\b|\bcanadian\b|united\s+kingdom|\bu\.?k\.?\b|\bengland\b"
    r"|\bscotland\b|\bireland\b|\bindia\b|\bgermany\b|\bfrance\b|\bspain\b"
    r"|\bitaly\b|\bportugal\b|\bpoland\b|\bswitzerland\b|\bnetherlands\b"
    r"|\bbelgium\b|\bsweden\b|\bnorway\b|\bdenmark\b|\bfinland\b|\baustria\b"
    r"|\bczech\b|\bromania\b|\bukraine\b|\bturkey\b|\bisrael\b|\begypt\b"
    r"|south\s+africa|\bnigeria\b|\bkenya\b|\bghana\b|\bmorocco\b"
    r"|\baustralia\b|new\s+zealand|\bsingapore\b|\bjapan\b|\bchina\b"
    r"|hong\s+kong|\btaiwan\b|south\s+korea|\bvietnam\b|\bthailand\b"
    r"|\bphilippines\b|\bindonesia\b|\bmalaysia\b|\bpakistan\b"
    r"|\bmexico\b|\bbrazil\b|\bargentina\b|\bcolombia\b|\bchile\b|\bperu\b"
    r"|\buae\b|united\s+arab\s+emirates|\bsaudi\b|\bqatar\b"
    r"|european\s+union|\bemea\b|\bapac\b|\blatam\b",
    re.IGNORECASE,
)

# "U.S. person" as ITAR/EAR defines it (citizen, permanent resident, refugee or
# asylee). Not the same question as work authorization — a time-limited status
# is authorized to work and is *not* a U.S. person — and a wrong answer has
# consequences past this application. Always parks, whatever the config says.
_EXPORT_CONTROL = re.compile(
    r"u\.?s\.?\s+person|export\s+control|itar\b|\bear\b\s+regulat", re.IGNORECASE
)
# A request to ELABORATE, not a question that can be answered yes/no. Asks which
# sponsorship you would need, not which status you hold, so `status_label` does
# not answer it either — these park.
#
# Deliberately not keyed on a leading "If": "If not, do you now or will you in
# the future need sponsorship...?" is a live Lever label and an ordinary,
# answerable sponsorship question. The elaboration request is what distinguishes
# these, not the conditional.
_FOLLOWUP = re.compile(
    r"if\s+you\s+(?:answered|selected)"
    r"|please\s+(?:share|provide|specify|elaborate|explain)"
    r"|(?:what|which)\s+(?:\w+\s+){0,2}(?:sponsorship|visa|permit)\s+"
    r"(?:would|do|will)"
    # Asks you to ENUMERATE, so a Yes/No is not an answer to it at all: "In what
    # countries do you have the unrestricted right to work?" is free text, and
    # letting the qualified branch write "No" into it put a nonsense answer in
    # front of an employer. Narrow on purpose — "What is your nationality?" is a
    # status question and is classified as one.
    r"|(?:what|which)\s+(?:\S+\s+){0,2}countr",
    re.IGNORECASE,
)
# Asks WHICH status or nationality you hold, rather than yes/no. Answered from
# `status_label` (free text) or `status_option_candidates` (a picker), never
# derived — `status: time_limited` covers OPT, H-1B and TN alike and cannot say
# which one applies.
_STATUS_DISCLOSURE = re.compile(
    r"^\s*work\s+(?:status|eligibility|authori[sz]\w*\s+status)"
    r"|(?:confirm|indicate|update|select)\s+(?:\S+\s+){0,6}authori[sz]\w*\s+status"
    r"|(?:what|which)\s+is\s+your\s+(?:visa|status)"
    r"|(?:update|confirm|indicate|select)\s+(?:\w+\s+){0,3}employ\w*\s+status"
    r"|currently\s+in\s+a\s+period\s+of",
    re.IGNORECASE,
)
# Nationality and citizenship are a COUNTRY, and `status_label` states a visa
# status. Answering "What is your nationality?" with "F-1 STEM OPT" is not a
# false legal claim but it is a nonsense answer in front of an employer, so these
# park. Checked before `_STATUS_DISCLOSURE`, which would otherwise claim them.
_NATIONALITY = re.compile(
    r"nationalit"
    r"|citizenship\s+in|hold\s+citizenship|citizenship\s+of"
    r"|(?:what|which)\s+is\s+your\s+citizenship"
    r"|are\s+you\s+(?:currently\s+)?an?\s+\w+\s+citizen"
    r"|citizen\s+of\s+a\s+country"
    r"|country\s+of\s+origin",
    re.IGNORECASE,
)
# "Second citizenship" is a different fact than `nationality` — the primary
# one — so it must not be answered from the same config value. Checked before
# the region/demonym match below, since a second-citizenship picker also
# offers a country list that would otherwise look like a direct-ask field.
_SECOND_NATIONALITY = re.compile(
    r"second\s+(?:country|nationality|citizenship)"
    r"|another\s+(?:country|nationality|citizenship)"
    r"|other\s+citizenship|dual\s+citizenship|additional\s+citizenship",
    re.IGNORECASE,
)
_NONE_CANDIDATES = ("None", "No", "N/A", "Not applicable")

# Region membership for "are you a citizen of a country in <region>?"-style
# yes/no questions. Not exhaustive — extend as real boards surface a region
# or country this doesn't cover, the same way `status_option_candidates`
# grows one entry at a time (HANDOFF.md item 7).
_SOUTH_ASIA = frozenset({
    "afghanistan", "bangladesh", "bhutan", "india", "maldives", "nepal",
    "pakistan", "sri lanka",
})
_SOUTHEAST_ASIA = frozenset({
    "brunei", "cambodia", "timor-leste", "indonesia", "laos", "malaysia",
    "myanmar", "philippines", "singapore", "thailand", "vietnam",
})
_EAST_ASIA = frozenset({
    "china", "hong kong", "japan", "macau", "mongolia", "north korea",
    "south korea", "taiwan",
})
_CENTRAL_ASIA = frozenset({
    "kazakhstan", "kyrgyzstan", "tajikistan", "turkmenistan", "uzbekistan",
})
_WESTERN_ASIA = frozenset({
    "armenia", "azerbaijan", "bahrain", "cyprus", "georgia", "iraq",
    "israel", "jordan", "kuwait", "lebanon", "oman", "palestine", "qatar",
    "saudi arabia", "syria", "turkey", "united arab emirates", "yemen",
})
_ASIA = (_SOUTH_ASIA | _SOUTHEAST_ASIA | _EAST_ASIA | _CENTRAL_ASIA
         | _WESTERN_ASIA)
_EU = frozenset({
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia",
    "denmark", "estonia", "finland", "france", "germany", "greece",
    "hungary", "ireland", "italy", "latvia", "lithuania", "luxembourg",
    "malta", "netherlands", "poland", "portugal", "romania", "slovakia",
    "slovenia", "spain", "sweden",
})
_EFTA = frozenset({"iceland", "liechtenstein", "norway", "switzerland"})

# Keys are matched as substrings of the normalized label, longest first, so
# "south asia" is tried before the broader "asia" it would otherwise also
# match. Values are the country-name sets above, in `_norm_option` form
# (lowercase, single-spaced).
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
_REGION_PHRASES_BY_LENGTH = tuple(
    sorted(_NATIONALITY_REGIONS, key=len, reverse=True)
)
# Demonym -> country, for "Are you currently an Indonesian citizen (WNI)?"
# style questions that name a country rather than a region. Same
# extend-as-surfaced policy as the region table above.
_DEMONYMS: dict[str, str] = {
    "indonesian": "indonesia", "indian": "india", "pakistani": "pakistan",
    "bangladeshi": "bangladesh", "sri lankan": "sri lanka",
    "nepali": "nepal", "bhutanese": "bhutan", "afghan": "afghanistan",
    "filipino": "philippines", "malaysian": "malaysia",
    "singaporean": "singapore", "thai": "thailand",
    "vietnamese": "vietnam", "burmese": "myanmar", "cambodian": "cambodia",
    "laotian": "laos", "bruneian": "brunei", "chinese": "china",
    "japanese": "japan", "mongolian": "mongolia", "taiwanese": "taiwan",
    "kazakh": "kazakhstan", "uzbek": "uzbekistan", "turkish": "turkey",
    "israeli": "israel", "emirati": "united arab emirates",
    "saudi": "saudi arabia",
}


def _nationality_region_match(label_norm: str, nationality_norm: str) -> bool | None:
    """Whether the configured nationality is inside the region/country a
    yes-no label names, or None if the label names neither — the caller
    parks on None rather than guessing."""
    for phrase in _REGION_PHRASES_BY_LENGTH:
        if phrase in label_norm:
            return nationality_norm in _NATIONALITY_REGIONS[phrase]
    for demonym, country in _DEMONYMS.items():
        if demonym in label_norm:
            return nationality_norm == country
    return None


_YES_NO = {True: "Yes", False: "No"}
# A board that renders "Yes." / "No." rather than "Yes" / "No" — two of them do.
# A punctuation variant is safe to try because `_pick_option` matches whole
# normalized labels; it can never reach a longer option that merely starts with
# the same word.
_YES_NO_VARIANTS = {True: ("Yes", "Yes."), False: ("No", "No.")}

WORK_AUTH_CATEGORIES = (
    "plain", "qualified", "alternation", "sponsorship", "compound_option",
    "names_other_country", "status_disclosure", "nationality", "export_control",
    "proof",
    "followup", "not_work_auth",
)


def _offers_bare_yes_no(options: tuple[str, ...]) -> bool:
    """Whether a bare "Yes"/"No" is on offer at all.

    When it is not, every option carries a claim past the polarity — group after
    group in the harvest offers three different sentences all starting "Yes,",
    meaning three different things. Answering one from a Yes/No would state
    whichever the board listed first.
    """
    bare = {"yes", "no"}
    return any(_norm_option(o).rstrip(".") in bare for o in options)


def classify_work_authorization(label: str, options: tuple[str, ...] = ()) -> str | None:
    """Which kind of work-authorization question this is, or None if the label
    is in the domain but matches no category.

    Classification only — it reads no config and picks no answer, so the same
    label always lands in the same category whatever the user's status is
    (R7: `src/` classifies and looks up; the clustering behind these categories
    was judgment, done once in a command session against the 237-board harvest
    and recorded in `tests/apply/fixtures/work_auth_labels.jsonl`).
    """
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
    sponsorship = (bool(_SPONSORSHIP_FAMILY.search(text))
                   and not _SPONSORSHIP_NEGATED.search(text))
    if authorized and sponsorship:
        # Both families read in the label. The governing verb settles it: "will
        # you require sponsorship to maintain your work authorization" is a
        # sponsorship question that happens to name authorization. Deliberately
        # narrow — four observed shapes, not an attempt to parse grammar.
        if _SPONSORSHIP_GOVERNS.search(text):
            authorized = False
        else:
            sponsorship = False
    if not (authorized or sponsorship):
        return None

    if authorized and not _NAMES_THE_US.search(text) and not _NAMES_US_ABBREV.search(text) \
            and _NAMES_OTHER_COUNTRY.search(text):
        # Only the authorization question is country-scoped. Needing sponsorship
        # is true wherever you are not already authorized, so "will you require
        # sponsorship to work in the UK?" is answerable and stays `sponsorship`.
        return "names_other_country"
    if options and not _offers_bare_yes_no(options):
        return "compound_option"
    if authorized and _AUTHORIZED_QUALIFIED.search(text):
        if _QUALIFIER_OFFERED_AS_ALTERNATIVE.search(text):
            return "alternation"
        return "qualified"
    return "sponsorship" if sponsorship else "plain"

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
    # `\bOPT\b` stays case-sensitive on purpose — lowercase "opt" is the verb,
    # and "opt out"/"opt in" appear all over a preferences file. Everything
    # else is case-insensitive like its two siblings; it was not, so a
    # perfectly ordinary lowercase bullet derived nothing and hard-failed
    # every submission.
    "time_limited": re.compile(
        r"(?i:\bF-1\b|STEM\s+extension|time[- ]limited\s+work\s+authori[sz]ation)"
        r"|\bOPT\b"
    ),
}
# A bullet that denies a status must not be read as stating it.
#
# Proximity-scoped, NOT whole-line. Dropping any line containing a negation
# anywhere reads "I am on F-1 OPT with STEM extension. I am not seeking
# relocation assistance." as stating nothing, which makes the status
# underivable and hard-fails every submission — a total block triggered by an
# unrelated clause. Only a negation in the run-up to the marker counts.
_NEGATION = re.compile(
    r"\b(?:not|never|no|neither|nor|isn'?t|aren'?t|don'?t|doesn'?t|won'?t"
    r"|cannot|can'?t)\b",
    re.IGNORECASE,
)
# How far back from the marker a negation still binds to it. Wide enough for
# "I do not currently hold a green card", short enough that a negation in a
# neighbouring sentence does not reach.
_NEGATION_WINDOW = 40


def _negated(line: str, marker_start: int) -> bool:
    """Whether the status marker at `marker_start` is being denied.

    Scoped to the marker's own clause: text after the nearest preceding
    sentence or clause break, and at most `_NEGATION_WINDOW` characters.
    """
    head = line[:marker_start]
    for boundary in (".", ";", ",", " - ", " — "):
        cut = head.rfind(boundary)
        if cut != -1:
            head = head[cut + len(boundary):]
    return bool(_NEGATION.search(head[-_NEGATION_WINDOW:]))
_PREFERENCES_SECTION = re.compile(
    r"^##\s+work\s+authori[sz]ation\s*$(?P<body>.*?)(?=^##\s|\Z)",
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
    match: tuple[str, ...]      # normalized keywords, tested as substrings
    answers: tuple[str, ...]    # candidates, in preference order
    exact: tuple[str, ...] = () # normalized labels, matched whole
    mode: str = "exact"         # "exact" (default) or "contains" — see _pick_option

    def matches(self, label: str) -> bool:
        """Whole-label first, then substring.

        Some questions have labels too short to keyword safely: "State" is a
        required dropdown on 3 of 39 boards, and a `state` substring also hits
        "United States" and "please state their name". Those need `exact`.
        """
        return label in self.exact or any(k in label for k in self.match)


@dataclass(frozen=True)
class Answers:
    identity: dict[str, str]
    education: dict[str, str]
    employment: dict[str, str] | None
    status: str
    rules: tuple[Rule, ...]
    scope_qualified_answer: str = "park"
    """How to answer an authorization question that narrows the scope —
    permanence, employer-independence, freedom from sponsorship. `park` (the
    default) hands it to the user. See `SCOPE_QUALIFIED_ANSWERS`."""
    status_label: str = ""
    """What to answer when a form asks WHICH status you hold rather than yes/no
    ("Work Authorization status (US Citizen, Green Card Holder, etc.)"). Not
    derivable: `status` groups OPT, H-1B and TN together and cannot say which
    applies. Empty (the default) parks the question."""
    status_option_candidates: tuple[str, ...] = ()
    """Option spellings that are true of you, in preference order, for a picker
    whose choices carry a claim past yes/no ("Yes, and I am currently in F-1
    status", "STEM OPT"). Matched by exact normalized label like every other
    candidate list, so an entry that no board offers simply never fires and the
    question parks. Consulted only by the work-authorization resolver."""
    nationality: str = ""
    """The country to answer "what is your nationality?" / "country of
    origin" / "select the country you hold citizenship in" with. Empty (the
    default) parks the question — unlike work authorization status, this is
    not derivable from anything else, so an unset value must never be
    guessed. Also drives region/demonym yes-no questions ("are you a citizen
    of a country in the EU/EFTA?"); see `_NATIONALITY_REGIONS`/`_DEMONYMS`."""
    second_nationality: str = ""
    """The country to answer a *second*-citizenship question with ("if you
    hold citizenship in a second country, select it, otherwise select
    None"). Empty (the default) answers "None"/"No" rather than parking,
    since holding no second nationality is itself the common, statable
    case — unlike `nationality`, silence here is an answer, not a gap."""
    sponsorship_followup_text: str = ""
    """Free-text answer for a conditional follow-up ("if yes, please provide
    details regarding your current visa status and future sponsorship
    needs") — a statement only the user can make, so never derived from
    `status`. Empty (the default) parks the question, same as `status_label`."""
    us_person_answer: str = "park"
    """How to answer a "U.S. person" export-control declaration (ITAR/EAR).
    `park` (the default) always hands it to the user — this is a legal
    declaration distinct from work authorization, and a wrong answer has
    consequences past this application. Only an explicit "yes"/"no" (never
    derived from `status`: a time-limited status is authorized to work and is
    NOT a U.S. person) lets it auto-fill. See `US_PERSON_ANSWERS`."""
    job_source: str = ""
    """This role's discovery source from state.yaml (`linkedin`, `indeed`,
    `greenhouse`, `lever`, `ashby`, ...) — not config, so callers set it after
    `load()` returns (see apply_cli.build()). Answers only "how did you hear
    about us" (see `_resolve_how_heard`): a listing JobSpy found on LinkedIn or
    Indeed genuinely was seen there regardless of which ATS ends up hosting the
    form, and a listing scraped straight off the company's own Greenhouse/
    Lever/Ashby board was genuinely found on that company's own career site."""
    parsed_salary: float | None = None
    """A salary figure computed from this job's own parsed compensation
    columns (`clean.parquet`'s `salary_min`/`salary_currency`, populated for
    Ashby postings via `includeCompensation=true`) times the vertical's
    `salary_expectation.markup_pct` — not config, and not the JD's own prose.
    Like `job_source`, callers set it after `load()` returns (see
    `apply_cli.build()`). `None` (the default: most postings carry no
    structured compensation, or the vertical has no `salary_expectation`
    block) leaves every money question to whatever already resolves it — a
    `rules:` match, or `/apply`'s JD-text scan/park. Set, it fills any
    `_MONEY_DOMAIN`-shaped question outright and supersedes a static
    `rules:` default, since a number this role's own posting states should
    always win over a generic configured fallback."""

    @property
    def employment_only_when_required(self) -> bool:
        """Whether to fill the employment block only where the board demands it.

        The block holds one role, and for anyone not currently employed that is
        the most recent one rather than a current one. Volunteering it on a
        board that left it optional implies a currency that may not hold, so
        this is a switch rather than a fixed policy — someone whose block *is*
        their current job wants it filled wherever it renders.
        """
        return bool((self.employment or {}).get("only_when_required"))

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
                 optional: tuple[str, ...] = (),
                 list_keys: tuple[str, ...] = ()) -> dict[str, str]:
    block = data.get(key)
    if not isinstance(block, dict):
        raise AnswersError(f"{key}: missing, or not a mapping")
    out = {}
    for field_key in required:
        out[field_key] = _require_str(block.get(field_key), f"{key}.{field_key}")
    for field_key in optional:
        if block.get(field_key) is not None:
            out[field_key] = _require_str(block[field_key], f"{key}.{field_key}")
    # A list-valued key holds alternate spellings of the same answer, for a
    # board whose taxonomy words it differently. Stored as a tuple, so a
    # consumer can offer them in order.
    for field_key in list_keys:
        raw = block.get(field_key)
        if raw is None:
            continue
        if not isinstance(raw, list) or not raw:
            raise AnswersError(f"{key}.{field_key}: must be a non-empty list")
        if not all(isinstance(v, str) and v.strip() for v in raw):
            raise AnswersError(
                f"{key}.{field_key}: every entry must be a non-empty string"
            )
        out[field_key] = tuple(v.strip() for v in raw)
    # The two boolean flags belong to `employment` alone. Subtracting them
    # unconditionally let `identity: {current_role: true}` and
    # `education: {only_when_required: true}` load clean and be discarded --
    # exactly the silently-missing-answer typo the unknown-key check exists
    # to catch.
    allowed = set(required) | set(optional) | set(list_keys)
    if key == "employment":
        allowed |= set(EMPLOYMENT_FLAGS)
    unknown = set(block) - allowed
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
        match = entry.get("match") or []
        exact_raw = entry.get("exact") or []
        for key, value in (("match", match), ("exact", exact_raw)):
            if not isinstance(value, list):
                raise AnswersError(f"{where}.{key}: must be a list")
        if not match and not exact_raw:
            raise AnswersError(f"{where}: needs a non-empty match or exact list")
        keywords = tuple(_norm(_require_str(k, f"{where}.match")) for k in match)
        exact = tuple(_norm(_require_str(k, f"{where}.exact")) for k in exact_raw)
        if any(not k for k in keywords + exact):
            raise AnswersError(f"{where}: a keyword normalizes to nothing")
        # `exact:` compares whole labels, so a short one is harmless; `match:`
        # is a substring test and a short keyword silently answers unrelated
        # questions. `C++` -> `c`, `C#` -> `c`, `A/B` -> `a b`.
        for original, keyword in zip(match, keywords):
            if len(keyword) < MIN_KEYWORD_LENGTH:
                raise AnswersError(
                    f"{where}.match: {original!r} normalizes to {keyword!r}, under "
                    f"{MIN_KEYWORD_LENGTH} characters. Matching is a substring "
                    f"test, so this would answer questions that have nothing to "
                    f"do with it — use `exact:` for the whole label instead."
                )
        for keyword in keywords + exact:
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
        mode = entry.get("mode", "exact")
        if mode not in ("exact", "contains"):
            raise AnswersError(f"{where}.mode: must be 'exact' or 'contains', got {mode!r}")
        unknown = set(entry) - {"match", "exact", "answer", "mode"}
        if unknown:
            raise AnswersError(f"{where}: unknown keys {sorted(unknown)}")
        rules.append(Rule(match=keywords, answers=answers, exact=exact, mode=mode))

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
            for a in rule.exact:
                if a in other.exact:
                    raise AnswersError(
                        f"rules[{i}] and rules[{j}] both match the exact label {a!r}"
                    )

    # An exact rule exists because the label is too short to keyword safely, so
    # a substring rule that also hits it defeats the point — and which one wins
    # would depend on file order.
    for i, rule in enumerate(rules):
        for j, other in enumerate(rules):
            if i == j:
                continue
            for label in rule.exact:
                for keyword in other.match:
                    if keyword in label:
                        raise AnswersError(
                            f"rules[{j}].match {keyword!r} also matches "
                            f"rules[{i}].exact {label!r}: one would shadow the other"
                        )
    return tuple(rules)


def preferences_statuses(text: str) -> set[str]:
    """Every work-authorization status the Work authorization section states.

    Reads *every* such section, not just the first — two sections stating
    different things must surface as the contradiction it is, rather than
    silently resolving to whichever came first.

    A line carrying a negation is ignored. "Not a permanent resident." and
    "I do not need sponsorship now or ever." both used to derive the status
    they deny, and this check is the only thing standing between a mistyped
    `status:` and a false answer on a legal question. Dropping the line makes
    it underivable, which is a hard error — the safe direction.
    """
    found: set[str] = set()
    for section in _PREFERENCES_SECTION.finditer(text or ""):
        for line in section.group("body").splitlines():
            for name, pattern in _PREFERENCES_MARKERS.items():
                hit = pattern.search(line)
                if hit and not _negated(line, hit.start()):
                    found.add(name)
    return found


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

    # The blocks are validated individually below, but nothing checked the top
    # level: `rules:` mistyped as `rulez:` loaded clean with zero rules, and
    # then every Tier B question parked with nothing saying why.
    unknown_top = set(data) - set(TOP_LEVEL_KEYS)
    if unknown_top:
        raise AnswersError(
            f"{p}: unknown top-level keys {sorted(unknown_top)} "
            f"(expected {sorted(TOP_LEVEL_KEYS)})"
        )

    identity = _parse_block(data, "identity", IDENTITY_KEYS,
                            list_keys=IDENTITY_LIST_KEYS)
    education = _parse_block(data, "education", EDUCATION_KEYS, EDUCATION_OPTIONAL_KEYS)

    employment = None
    if data.get("employment") is not None:
        employment = _parse_block(data, "employment", EMPLOYMENT_KEYS)
        for flag in EMPLOYMENT_FLAGS:
            employment[flag] = bool(data["employment"].get(flag))

    work_auth = data.get("work_authorization")
    if not isinstance(work_auth, dict):
        raise AnswersError("work_authorization: missing, or not a mapping")
    unknown_auth = set(work_auth) - set(WORK_AUTHORIZATION_KEYS)
    if unknown_auth:
        raise AnswersError(
            f"work_authorization: unknown keys {sorted(unknown_auth)} "
            f"(expected {sorted(WORK_AUTHORIZATION_KEYS)})"
        )
    status = _require_str(work_auth.get("status"), "work_authorization.status")
    if status not in WORK_AUTHORIZATION_STATUSES:
        raise AnswersError(
            f"work_authorization.status: {status!r} is not one of "
            f"{sorted(WORK_AUTHORIZATION_STATUSES)}"
        )
    # Normalized rather than coerced: PyYAML reads a bare `no` as the boolean
    # False, and silently mapping that to "no" would let a typo'd `yes`/`no`
    # decide a legal answer. Quoting is required and the error says so.
    scope_qualified = work_auth.get("scope_qualified_answer", "park")
    if scope_qualified not in SCOPE_QUALIFIED_ANSWERS:
        raise AnswersError(
            f"work_authorization.scope_qualified_answer: {scope_qualified!r} is "
            f"not one of {list(SCOPE_QUALIFIED_ANSWERS)} — quote it, since YAML "
            f"reads a bare yes/no as a boolean"
        )
    status_label = work_auth.get("status_label", "")
    if not isinstance(status_label, str):
        raise AnswersError(
            f"work_authorization.status_label: must be a string, got "
            f"{type(status_label).__name__}"
        )
    raw_candidates = work_auth.get("status_option_candidates", []) or []
    if not isinstance(raw_candidates, list) or not all(
            isinstance(c, str) and c.strip() for c in raw_candidates):
        raise AnswersError(
            "work_authorization.status_option_candidates: must be a list of "
            "non-empty strings, each the exact option text a board offers"
        )
    nationality = work_auth.get("nationality", "")
    if not isinstance(nationality, str):
        raise AnswersError(
            f"work_authorization.nationality: must be a string, got "
            f"{type(nationality).__name__}"
        )
    second_nationality = work_auth.get("second_nationality", "")
    if not isinstance(second_nationality, str):
        raise AnswersError(
            f"work_authorization.second_nationality: must be a string, got "
            f"{type(second_nationality).__name__}"
        )
    sponsorship_followup_text = work_auth.get("sponsorship_followup_text", "")
    if not isinstance(sponsorship_followup_text, str):
        raise AnswersError(
            f"work_authorization.sponsorship_followup_text: must be a string, "
            f"got {type(sponsorship_followup_text).__name__}"
        )
    # Normalized rather than coerced, same reasoning as scope_qualified_answer:
    # PyYAML reads a bare `no` as the boolean False, and this is a legal
    # declaration -- a silently mis-typed value must fail loud, not guess.
    us_person_answer = work_auth.get("us_person_answer", "park")
    if us_person_answer not in US_PERSON_ANSWERS:
        raise AnswersError(
            f"work_authorization.us_person_answer: {us_person_answer!r} is not "
            f"one of {list(US_PERSON_ANSWERS)} — quote it, since YAML reads a "
            f"bare yes/no as a boolean"
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
        scope_qualified_answer=scope_qualified,
        status_label=status_label.strip(),
        status_option_candidates=tuple(c.strip() for c in raw_candidates),
        nationality=nationality.strip(),
        second_nationality=second_nationality.strip(),
        sponsorship_followup_text=sponsorship_followup_text.strip(),
        us_person_answer=us_person_answer,
    )


# ---------------------------------------------------------------- resolving


def _pick_option(
    field: MergedField, candidates: tuple[str, ...], mode: str = "exact"
) -> str | None:
    """The first candidate the widget actually offers.

    With no option list — every DOM-only react-select, `country` included —
    there is nothing to check against, so the first candidate goes through and
    fill.py's post-selection assert is what catches a string the widget rejects.

    `mode="contains"` matches a candidate that appears anywhere inside an
    offered option's text (e.g. candidate "Yes" inside option "Yes, I am able
    and willing to work..."), for a rule whose keyword is decided but whose
    candidate answers are short affirmations that never equal a board's own
    full-sentence option. Still first-candidate-wins, same as exact mode —
    just a looser test per candidate.
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
    option. Public so `plan.py` can hold an `--answers` override to the same
    standard every deterministic path already meets — matching by exact
    normalized label, and canonicalizing to what the board actually renders.

    Returns the candidate unchanged when the field carries no option list, for
    the same reason `_pick_option` does: nothing to check against, and
    fill.py's post-selection assert is the real check there.
    """
    return _pick_option(field, (candidate,))


def _resolve_choice(field: MergedField, candidates: tuple[str, ...], tier: str,
                    what: str, mode: str = "exact") -> Resolution:
    picked = _pick_option(field, candidates, mode=mode)
    if picked is None:
        offered = [o.label for o in field.options]
        return _park(f"{what}: none of {list(candidates)} is offered ({offered})", tier)
    return _fill((picked,) if field.multi else picked, tier)


def _pick_country(field: MergedField, value: str) -> str | None:
    """The country option, allowing for the dial code Greenhouse appends.

    `#country` is not a country field. On 24 of 24 live boards it renders inside
    `phone-input__country` — it is the phone number's dial-code selector, and
    its options read "United States +1". So an exact match first, then the one
    option that is the country followed by " +".

    That second rule is exact, not fuzzy: only the dial code can follow, so
    "United States" cannot reach "United States Minor Outlying Islands +246".
    More than one match is still refused.
    """
    if not field.options:
        return value
    wanted = _norm_option(value)
    for option in field.options:
        if _norm_option(option.label) == wanted:
            return option.label
    hits = [o.label for o in field.options
            if _norm_option(o.label).startswith(f"{wanted} +")]
    return hits[0] if len(hits) == 1 else None


def _resolve_date(field: MergedField) -> Resolution | None:
    """A board's own `Date`-typed field (kind `date`, currently Ashby-only),
    resolved to today rather than a Tier B free-text default.

    Ashby's `react-datepicker` widget parses whatever is typed on blur and
    silently clears the input if it cannot — a Tier B sentence like
    "Immediately." reads back fine right after `fill()` (before blur fires)
    and then vanishes, leaving the board showing its empty picker with no
    error anywhere. The format is `DATE_FORMAT`, which is where the evidence
    for it is recorded.
    """
    if field.kind != "date":
        return None
    return _fill(_today().strftime(DATE_FORMAT), "A")


def _resolve_identity(field: MergedField, answers: Answers) -> Resolution | None:
    if field.id in FILE_IDS:
        return Resolution("defer", tier="A", reason=field.id)
    if field.id in ("full_name", "name", "_systemfield_name"):
        # A board-shape difference, not a Greenhouse concept: Lever/Ashby ask
        # one combined "Full name" field where Greenhouse asks first/last
        # separately. Composed here rather than aliased through
        # _IDENTITY_IDS, since there is no single identity.* key to alias to.
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
                f"identity.country: {value!r} matches none of "
                f"{[o.label for o in field.options]}", "A"
            )
        return _fill(picked, "A")
    if field.kind == "react_select":
        return _resolve_choice(field, _identity_candidates(field.id, value, answers, key),
                               "A", f"identity.{key}")
    return _fill(value, "A")


#: The one field with a second candidate, and the only one there is evidence
#: for. Greenhouse's `candidate-location` and Lever's `location` map to the
#: same config key and are deliberately NOT here: measured, they carry no
#: option list at plan time, so widening the key would only change behaviour
#: at fill time, on the highest-volume lane, on no evidence at all.
_COUNTRY_FALLBACK_IDS = frozenset({"_systemfield_location"})


def _identity_candidates(field_id: str, value: str, answers: Answers,
                         key: str = "") -> tuple[str, ...]:
    """The values to offer a chooser for this identity field, best first.

    Only Ashby's location field has more than one, because that id is two
    different questions depending on the board: city-level, where a canonical
    "City, State, Country" matches exactly, and **country-only**, where a city
    name returns no options at all and "United States" does. Nothing in either
    payload says which.

    Falling back to the configured country is not a looser match — it is the
    answer to the coarser question the board is actually asking, stated in the
    config already. Same "first candidate the board offers wins" rule as a
    Tier B answer list, and a board offering neither still parks.

    Keyed on the DOM id, never on the label: `identity.country` also feeds
    Greenhouse's phone dial-code widget, which is not a country question.
    """
    if key == "location":
        # Configured alternates first-class, not a fuzzy match: a board whose
        # taxonomy words the same place differently is answered from config,
        # and a board offering none of them still parks rather than guessing.
        # Guessing here is how an application goes out naming the wrong place —
        # city names repeat across countries, and a live board was measured
        # offering five same-named cities in five of them.
        alternates = answers.identity.get("location_alternates") or ()
        candidates = (value, *(a for a in alternates if a != value))
        if field_id in _COUNTRY_FALLBACK_IDS:
            candidates = (*candidates, answers.identity["country"])
        return candidates
    if field_id in _COUNTRY_FALLBACK_IDS:
        return (value, answers.identity["country"])
    return (value,)


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


def _today() -> date:
    """Indirected so a test can pin the date without freezing the clock."""
    return date.today()


def _resolve_eeoc(field: MergedField, answers: Answers) -> Resolution:
    if field.id in _EEOC_SIGNATURE_IDS:
        first, last = answers.identity["first_name"], answers.identity["last_name"]
        return _fill(f"{first} {last}".strip(), "A2")
    if field.id in _EEOC_SIGNATURE_DATE_IDS:
        return _fill(_today().strftime(DATE_FORMAT), "A2")
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


def _resolve_nationality(field: MergedField, answers: Answers) -> Resolution:
    """Nationality/citizenship questions, split by shape so one config value
    is never asked to answer three different facts (direct ask, second
    citizenship, region/country yes-no) — see the field docstrings on
    `Answers.nationality`/`second_nationality`."""
    nationality = answers.nationality
    if not nationality:
        return _park(
            "work authorization: asks your nationality or citizenship, which is "
            "a country rather than the visa status `status_label` states, and "
            "no work_authorization.nationality is configured",
            "B0",
        )
    label = field.label or ""
    nationality_norm = _norm_option(nationality)

    if _SECOND_NATIONALITY.search(label):
        second = answers.second_nationality
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
    if _offers_bare_yes_no(options):
        match = _nationality_region_match(_norm_option(label), nationality_norm)
        if match is not None:
            picked = _pick_option(field, _YES_NO_VARIANTS[match])
            if picked is not None:
                return _fill(picked, "B0")
        return _park(
            "nationality: this yes/no question names a country or region not "
            "in the configured lookup — extend it rather than guess",
            "B0",
        )

    picked = _pick_option(field, (nationality,))
    if picked is None:
        return _park(
            f"nationality: {nationality!r} is not among the offered options",
            "B0",
        )
    return _fill(picked, "B0")


def _resolve_work_authorization(field: MergedField, answers: Answers) -> Resolution:
    label = field.label or ""
    options = tuple(o.label for o in field.options)
    category = classify_work_authorization(label, options)

    if category == "names_other_country":
        return _park(
            "work authorization: the question names a country other than "
            "the US, which this status does not answer",
            "B0",
        )
    if category == "export_control":
        if answers.us_person_answer == "park":
            return _park(
                "work authorization: this asks whether you are a \"U.S. "
                "person\" as the export-control rules define it, which is a "
                "different question from work authorization and is yours to "
                "answer. Set work_authorization.us_person_answer to answer "
                "this without review",
                "B0",
            )
        value = answers.us_person_answer == "yes"
        if not field.options:
            return _fill(_YES_NO[value], "B0")
        picked = _pick_option(field, _YES_NO_VARIANTS[value])
        if picked is not None:
            return _fill((picked,) if field.multi else picked, "B0")
        return _park(
            "work authorization: us_person_answer is set, but this board "
            "offers no bare Yes/No option for the \"U.S. person\" question",
            "B0",
        )
    if category == "nationality":
        return _resolve_nationality(field, answers)
    if category == "followup":
        if answers.sponsorship_followup_text and not field.options:
            return _fill(answers.sponsorship_followup_text, "B0")
        return _park(
            "work authorization: a conditional follow-up asking which "
            "sponsorship you would need. Set "
            "work_authorization.sponsorship_followup_text to answer these "
            "without review",
            "B0",
        )
    if category in ("status_disclosure", "compound_option"):
        return _resolve_status_claim(field, answers, category)
    if category == "alternation":
        # The qualifier is one option among several, so the narrow reading
        # `scope_qualified_answer` settles is not the question being asked.
        # Parked whatever the setting says.
        return _park(
            "work authorization: the scope qualifier is offered as an "
            "alternative (\"permanent or temporary\", \"with or without\"), "
            "so it does not narrow the question the way "
            "scope_qualified_answer assumes",
            "B0",
        )
    if category is None:
        return _park("work authorization: label matches no answerable family", "B0")

    if category == "qualified":
        # Only a time-limited status makes these unanswerable. For a citizen
        # or permanent resident, "permanent basis", "any employer" and
        # "without sponsorship" are all knowable and all Yes — parking them
        # blocked 7 of 89 harvested boards for the wrong user.
        if answers.status == "citizen_or_pr":
            value = answers.authorized_now
        elif answers.scope_qualified_answer == "park":
            return _park(
                "work authorization: the question qualifies the scope "
                "(permanence / any employer / without sponsorship), which this "
                "status alone cannot answer. Set "
                "work_authorization.scope_qualified_answer to answer these "
                "without review",
                "B0",
            )
        else:
            # The user's own stated answer, not an inference from `status` (R7:
            # src/ reads the preference, it does not form the judgment).
            value = answers.scope_qualified_answer == "yes"
    elif category == "sponsorship":
        value = answers.requires_sponsorship
    else:                                   # plain, proof
        value = answers.authorized_now

    if not field.options:
        # A yes/no question rendered as a text box — 11 groups in the harvest,
        # every one of them ordinary. Writing the word is the same answer.
        return _fill(_YES_NO[value], "B0")
    picked = _pick_option(field, _YES_NO_VARIANTS[value])
    if picked is not None:
        return _fill((picked,) if field.multi else picked, "B0")
    # The board offers no bare Yes/No for the polarity this resolves to — the
    # "Yes" branch is split across several qualified sentences. Only the user's
    # own list can say which is true of them.
    return _resolve_status_claim(field, answers, "compound_option")


def _resolve_status_claim(field: MergedField, answers: Answers,
                          category: str) -> Resolution:
    """Answer from the user's own stated facts, or park.

    Two shapes, one source. `status_disclosure` asks which status you hold;
    `compound_option` offers choices that each carry a claim past yes/no. Both
    are statements only the user can make, so `src/` looks them up and never
    infers them from `status` (R7).
    """
    if field.options:
        picked = (_pick_option(field, answers.status_option_candidates)
                  if answers.status_option_candidates else None)
        if picked is not None:
            return _fill((picked,) if field.multi else picked, "B0")
        offered = [o.label for o in field.options]
        return _park(
            f"work authorization: none of the options states your status. Add "
            f"the true one to work_authorization.status_option_candidates "
            f"(offered: {offered})",
            "B0",
        )
    if answers.status_label:
        return _fill(answers.status_label, "B0")
    return _park(
        "work authorization: asks which status you hold, which `status` alone "
        "does not say. Set work_authorization.status_label to answer these "
        "without review",
        "B0",
    )


# Same phrasing the static "how did you hear" rule in application_answers.yaml
# matches on — kept in sync deliberately, since this resolver only ever runs
# as that rule's first attempt, never on its own.
_HOW_HEARD_PHRASES = ("how did you hear", "where did you hear",
                      "how did you learn", "become aware")

# JobSpy-aggregator sources: the listing genuinely was seen on that site, no
# matter which ATS ends up hosting the application form itself.
_AGGREGATOR_SOURCE_LABELS = {"linkedin": "LinkedIn", "indeed": "Indeed"}

# Sources that mean the row was scraped straight off the company's own board
# rather than a third-party aggregator — Workday excluded, since apply/ never
# submits to one (discovery/scoring only; see CLAUDE.md).
_OWN_BOARD_SOURCES = frozenset({"greenhouse", "lever", "ashby"})

# Substrings of a board's own option label that mean "found on our own site",
# tried in order against whatever the board actually offers.
_OWN_BOARD_OPTION_HINTS = ("career site", "careers page", "company website",
                          "company career site")


def _resolve_how_heard(field: MergedField, answers: Answers) -> Resolution | None:
    """"How did you hear about us" from this role's own discovery source.

    Deterministic and job-specific rather than a fixed generic default: a
    listing JobSpy found on LinkedIn/Indeed was genuinely seen there, and one
    scraped straight off the company's own Greenhouse/Lever/Ashby board was
    genuinely found on that company's own career site. Only fires when the
    label matches; returns None (falls through to the static `rules:` list)
    on every other source, or when nothing it tries is among the board's
    offered options.
    """
    if not field.options and field.kind != "react_select":
        return None
    if not any(phrase in _norm(field.label) for phrase in _HOW_HEARD_PHRASES):
        return None
    source = answers.job_source.strip().lower()
    if source in _AGGREGATOR_SOURCE_LABELS:
        candidates = (_AGGREGATOR_SOURCE_LABELS[source],)
    elif source in _OWN_BOARD_SOURCES:
        candidates = _OWN_BOARD_OPTION_HINTS
    else:
        return None
    picked = _pick_option(field, candidates, mode="contains")
    return _fill(picked, "B") if picked is not None else None


def _resolve_parsed_salary(field: MergedField, answers: Answers) -> Resolution | None:
    """A money question, answered from `answers.parsed_salary` when this job's
    own parsed compensation data set one — deterministic, so it runs ahead of
    `_resolve_rule` and supersedes a static `rules:` default outright, same as
    `/apply`'s JD-text scan does for the cases that reach it (see
    `Answers.parsed_salary`). Untouched (returns `None`) whenever no figure was
    computed, which is the common case."""
    if answers.parsed_salary is None:
        return None
    if not _MONEY_DOMAIN.search(field.label or ""):
        return None
    if field.kind == "file":
        return None
    figure = round(answers.parsed_salary)
    if field.options or field.kind == "react_select":
        return _resolve_choice(field, (str(figure), f"${figure:,}"), "PARSED", "rule")
    return _fill(str(figure), "PARSED")


def _resolve_rule(field: MergedField, answers: Answers) -> Resolution | None:
    label = _norm(field.label)
    if not label:
        return None
    how_heard = _resolve_how_heard(field, answers)
    if how_heard is not None:
        return how_heard
    for rule in answers.rules:
        if rule.matches(label):
            if field.kind == "file":
                return (_park("a rule cannot answer a file upload", "B")
                        if field.required
                        else _skip("optional file upload, no rule can fill it", "B"))
            if field.options or field.kind == "react_select":
                return _resolve_choice(field, rule.answers, "B", "rule", mode=rule.mode)
            return _fill(rule.answers[0], "B")
    return None


def resolve(field: MergedField, answers: Answers) -> Resolution:
    """Resolve one reconciled field. Never raises on content — an unanswerable
    field comes back parked (required) or skipped (optional)."""
    if field.section == "eeoc":
        return _resolve_eeoc(field, answers)
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

    resolved_date = _resolve_date(field)
    if resolved_date is not None:
        return resolved_date

    # `_NOT_WORK_AUTH` first: the widened domain regex catches an age check, a
    # clearance question and a relocation-cities checkbox that must keep falling
    # through to tier C, where the /apply session answers them as ordinary
    # questions. A B0 park would make the whole role manual-apply instead.
    if (WORK_AUTHORIZATION_DOMAIN.search(field.label or "")
            and not _NOT_WORK_AUTH.search(field.label or "")):
        resolution = _resolve_work_authorization(field, answers)
        # Blank is never a false claim. "What visa type do you hold? (If
        # applicable)" is optional free text on the boards that ask it, and
        # parking a role over it would cost more than it protects.
        if resolution.parked and not field.required:
            return _skip(resolution.reason, "B0")
        return resolution

    parsed_salary = _resolve_parsed_salary(field, answers)
    if parsed_salary is not None:
        return parsed_salary

    rule = _resolve_rule(field, answers)
    if rule is not None:
        return rule

    # Tier C: /apply decides whether this is draftable free text or a "why us"
    # question. Either way nothing deterministic can fill it.
    return (_park("no rule matches this question", "C")
            if field.required else _skip("optional, no rule matches", "C"))
