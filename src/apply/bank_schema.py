"""The answer bank's schema: loading and validating
`profile/application_answers.yaml` into `Answers`, filling the facts it leaves
out from resume.yaml, and the label normalization both wordings and labels are
compared in. Fails loud on anything ambiguous."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.apply.places import canonical_place
from src.shared import paths

EXAMPLE_NAME = "application_answers.example.yaml"

SCHEMA_VERSION = 3

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
)

# A keyword this short matches almost any label. Punctuation is stripped before
# matching, so `C++` normalizes to the single character `c`.
MIN_KEYWORD_LENGTH = 3

# Answers a question may compute rather than state.
COMPUTED = ("salary", "start_date", "how_heard")


#: `cover_letter:` values. `when_offered` attaches a letter wherever the board
#: has a cover-letter upload; `when_required` only where it requires one.
COVER_LETTER_POLICIES = ("when_required", "when_offered")

# The demographic topics `demographics.answers` may state. Any other
# demographic question (sexual orientation, transgender, pronouns) opts out.
DEMOGRAPHIC_TOPICS = ("gender", "race", "hispanic_ethnicity", "veteran_status", "disability_status")


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


# Option labels too generic to carry a work-authorization claim of their own.
_BARE_OPTIONS = frozenset({"yes", "no", "yes.", "no.", "other", "n/a"})


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
class Salary:
    """The figure a salary question asks for in one country."""

    amount: float
    currency: str = ""
    markup_pct: float | None = None
    """Your own country only: a posting's minimum in `currency` times this, ahead of `amount`."""
    lanes: tuple[str, ...] = ()
    """The lanes it answers for; empty is every lane."""

    def for_lane(self, lane: str) -> bool:
        return not self.lanes or lane in self.lanes


@dataclass(frozen=True)
class CountryFacts:
    authorized: bool
    requires_sponsorship: bool
    unrestricted: bool = False
    status_label: str = ""
    followup: str = ""
    start_in_days: int | None = None
    visa_salary_floor: SalaryFloor | None = None
    salary: Salary | None = None


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
    missing = [k for k in required if block.get(k) is None]
    if missing:
        raise AnswersError(
            f"{key}.{missing[0]}: not set, here or in resume.yaml (which sets it when "
            "it can read it: name, contact, education[0], the latest work context)"
        )
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
    "salary",
)
_SALARY_FLOOR_KEYS = ("amount", "currency", "per", "note")
_SALARY_FLOOR_PER = ("year", "month")
_SALARY_KEYS = ("amount", "currency", "markup_pct", "lanes")
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


def _parse_salary(raw, where: str) -> Salary | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AnswersError(f"{where}: must be {{amount[, currency, markup_pct, lanes]}}")
    unknown = set(raw) - set(_SALARY_KEYS)
    if unknown:
        raise AnswersError(f"{where}: unknown keys {sorted(unknown)}")
    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        raise AnswersError(f"{where}.amount: must be a positive number")
    currency = raw.get("currency", "")
    if currency and (not isinstance(currency, str) or not _CURRENCY.match(currency)):
        raise AnswersError(f"{where}.currency: must be a three-letter ISO code, e.g. EUR")
    markup = raw.get("markup_pct")
    if markup is not None:
        if isinstance(markup, bool) or not isinstance(markup, (int, float)) or markup < 0:
            raise AnswersError(f"{where}.markup_pct: must be a number >= 0")
        if not currency:
            raise AnswersError(f"{where}: markup_pct needs the posting currency it applies to")
    lanes = raw.get("lanes") or []
    if not isinstance(lanes, list) or not all(isinstance(x, str) and x.strip() for x in lanes):
        raise AnswersError(f"{where}.lanes: must be a list of lane names")
    return Salary(
        amount=amount,
        currency=currency,
        markup_pct=markup,
        lanes=tuple(x.strip() for x in lanes),
    )


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
            salary=_parse_salary(row.get("salary"), f"{where}.salary"),
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


def resume_for(bank_path: Path) -> Path:
    """The resume.yaml beside this bank (resume.example.yaml beside the example), else paths.RESUME."""
    name = "resume.example.yaml" if ".example." in bank_path.name else "resume.yaml"
    sibling = bank_path.parent / name
    return sibling if sibling.is_file() else paths.RESUME


def with_resume(data: dict, resume: dict) -> dict:
    """data with every identity, education and employment key it leaves out
    filled from resume.yaml. Employment only when the bank has the block."""
    from src.apply import resume_facts

    out = dict(data)
    for key, facts in (
        ("identity", resume_facts.identity),
        ("education", resume_facts.education),
        ("employment", resume_facts.employment),
    ):
        block = data.get(key)
        if key == "employment" and key not in data:
            continue
        if block is None:
            block = {}
        if isinstance(block, dict):
            out[key] = {**facts(resume), **block}
    return out


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
    return answers_from(data, where=str(p), resume_path=resume_for(p))


def answers_from(data, where: str = "answer bank", resume_path: Path | None = None) -> Answers:
    """The bank `data` as Answers; identity, education and employment keys it
    leaves out come from resume.yaml (`resume_path`, default paths.RESUME)."""
    from src.apply import resume_facts

    if not isinstance(data, dict):
        raise AnswersError(f"{where}: top level must be a mapping")
    if data.get("schema_version") == 2:
        raise AnswersError(
            f"{where}: schema_version 2: identity, education and employment now come from "
            "resume.yaml (schema 3). Run `uv run config migrate --dry-run`, then --apply"
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise AnswersError(f"{where}: schema_version must be {SCHEMA_VERSION}")
    data = with_resume(data, resume_facts.read(resume_path or paths.RESUME))
    if "browser" in data:
        raise AnswersError(
            f"{where}: browser: moved to profile/settings.yaml. Run "
            "`uv run config migrate --dry-run`, then --apply"
        )
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
    home = canonical_place(identity["country"]) or identity["country"]
    for name, row in countries.items():
        if row.salary and row.salary.markup_pct is not None and name != home:
            raise AnswersError(
                f"work_authorization.countries.{name}.salary.markup_pct: only your own "
                f"country's row ({home}) takes a markup"
            )

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


def in_work_authorization_domain(label: str) -> bool:
    """Whether a label goes to the work-authorization resolver."""
    return bool(
        WORK_AUTHORIZATION_DOMAIN.search(label or "") and not _NOT_WORK_AUTH.search(label or "")
    )
