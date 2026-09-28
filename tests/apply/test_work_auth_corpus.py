"""The work-authorization golden corpus: every real label, with its category.

`work_auth_labels.jsonl` is every distinct (label, options) pair that read as
work-authorization-shaped across a 237-board harvest, plus rows added when a
live run surfaced a phrasing the harvest missed. The `category` column is
judgment, clustered once in a command session (R7 keeps it out of `src/`);
everything below is mechanical.

Two assertions per row, deliberately separate:

1. `classify_work_authorization` puts the label in its recorded category.
2. `resolve` does what that category's policy says, given the bank's facts for
   the question's country.

`not_work_auth` rows mention sponsorship or eligibility and are not
work-authorization questions at all; they must fall through to the question
bank, never park at B0.
"""
from __future__ import annotations

import json
from dataclasses import replace as dc_replace

import pytest

from src.apply.answers import (
    WORK_AUTH_CATEGORIES,
    CountryFacts,
    classify_work_authorization,
    resolve,
)
from src.apply.reconcile import MergedField, MergedOption

from .conftest import FIXTURES

CORPUS = [
    json.loads(line)
    for line in (FIXTURES / "work_auth_labels.jsonl").read_text(
        encoding="utf-8").splitlines()
    if line.strip()
]

STATUS_CLAIM = ("status_disclosure", "compound_option")

OPT = CountryFacts(authorized=True, requires_sponsorship=True,
                   status_label="F-1 STEM OPT",
                   followup="Currently authorized to work under F-1 STEM OPT; will "
                            "require H-1B sponsorship upon its expiration.")
CITIZEN = CountryFacts(authorized=True, requires_sponsorship=False, unrestricted=True)
ABROAD = CountryFacts(authorized=False, requires_sponsorship=True)

CLAIMS = {
    "f-1": {"holds": True, "country": "United States"},
    "f1": {"holds": True, "country": "United States"},
    "stem opt": {"holds": True, "country": "United States"},
    "i am authorized to work in the united states for any employer":
        {"authorized": True, "country": "United States"},
    "i require sponsorship in the country of my application now or in the future":
        {"requires_sponsorship": True},
    "yes, and i am currently in f-1 status (if you currently have opt or "
    "stem-ext opt or cpt, you will select this answer.)":
        {"authorized": True, "holds": True, "country": "United States"},
    "i am authorized to work lawfully in the united states and do require "
    "future sponsorship.":
        {"authorized": True, "requires_sponsorship": True, "country": "United States"},
}


def ids(rows):
    return [f"{r['category']}:{r['label'][:60]}" for r in rows]


def rows_for(*categories):
    return [r for r in CORPUS if r["category"] in categories]


def field_of(row) -> MergedField:
    return MergedField(
        id="q", name="q", label=row["label"], required=row["required"],
        kind=row["kind"], section="questions", multi=row["multi"],
        api_type="multi_value_single_select" if row["options"] else "input_text",
        options=tuple(MergedOption(value=o, label=o) for o in row["options"]),
    )


def says(value, word: str) -> bool:
    got = value[0] if isinstance(value, tuple) else value
    return str(got).strip().rstrip(".").casefold() == word.casefold()


def offers(row, word: str) -> bool:
    if not row["options"]:
        return True
    return any(o.strip().rstrip(".").casefold() == word.casefold()
               for o in row["options"])


def names_only_the_us(row) -> bool:
    from src.apply.answers import places_named
    return places_named(row["label"]) in ([], ["United States"])


@pytest.fixture
def opt_holder(answers):
    """An F-1 OPT holder in the US, a citizen of India, not authorized elsewhere."""
    return dc_replace(answers, countries={"default": ABROAD, "United States": OPT,
                                          "India": CITIZEN},
                      nationalities=("India",), claims=CLAIMS, job_country="United States")


@pytest.fixture
def us_citizen(answers):
    return dc_replace(answers, countries={"default": ABROAD, "United States": CITIZEN},
                      job_country="United States")


class TestTheCorpusIsWellFormed:
    def test_every_row_names_a_known_category(self):
        unknown = {r["category"] for r in CORPUS} - set(WORK_AUTH_CATEGORIES)
        assert not unknown

    def test_the_corpus_is_the_size_it_claims(self):
        assert len(CORPUS) == 177
        assert sum(r["boards"] for r in CORPUS) == 249

    def test_rows_are_unique_on_label_and_options(self):
        keys = [(r["norm_label"], tuple(r["options"])) for r in CORPUS]
        assert len(keys) == len(set(keys))


class TestClassification:
    @pytest.mark.parametrize("row", CORPUS, ids=ids(CORPUS))
    def test_the_label_lands_in_its_recorded_category(self, row):
        got = classify_work_authorization(row["label"], tuple(row["options"]))
        assert got == row["category"], f"classified {got!r}, corpus says {row['category']!r}"


class TestNotWorkAuthStaysOutOfTheResolver:
    ROWS = rows_for("not_work_auth")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_never_parks_at_b0(self, row, opt_holder):
        assert resolve(field_of(row), opt_holder).tier != "B0"


class TestExportControl:
    ROWS = rows_for("export_control")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_parks_unless_us_person_is_stated(self, row, opt_holder):
        r = resolve(field_of(row), opt_holder)
        assert r.action == ("park" if row["required"] else "skip")
        assert r.tier == "B0"

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_a_stated_no_fills_only_a_bare_yes_no_us_person_question(self, row, opt_holder):
        r = resolve(field_of(row), dc_replace(opt_holder, us_person="no"))
        if "person" in row["label"].casefold() and offers(row, "No"):
            assert r.action == "fill" and says(r.value, "No")
        else:
            assert r.action == ("park" if row["required"] else "skip")


class TestNationality:
    ROWS = {r["label"]: r for r in rows_for("nationality")}

    def test_every_row_parks_without_a_nationality(self, answers):
        for row in self.ROWS.values():
            r = resolve(field_of(row), dc_replace(answers, nationalities=()))
            assert r.action == ("park" if row["required"] else "skip"), row["label"]

    def test_direct_ask_free_text(self, opt_holder):
        r = resolve(field_of(self.ROWS["What is your nationality?"]), opt_holder)
        assert r.action == "fill" and r.value == "India"

    def test_direct_ask_country_dropdown(self, opt_holder):
        row = self.ROWS["Please select the country you hold citizenship in"]
        r = resolve(field_of(row), opt_holder)
        assert r.action == "fill" and r.value.strip() == "India"

    SECOND = ("If you hold citizenship in a second country, please select it "
              "below. Otherwise, select None.")

    def test_second_citizenship_is_none_with_one_nationality(self, opt_holder):
        r = resolve(field_of(self.ROWS[self.SECOND]), opt_holder)
        assert r.action == "fill" and r.value == "None"

    def test_second_citizenship_fills_the_second_nationality(self, opt_holder):
        a = dc_replace(opt_holder, nationalities=("India", "Canada"))
        r = resolve(field_of(self.ROWS[self.SECOND]), a)
        assert r.action == "fill" and r.value.strip() == "Canada"

    EU = "Are you a citizen of a country in the EU/EFTA?✱"

    def test_region_yes_no_is_no_outside_the_region(self, opt_holder):
        r = resolve(field_of(self.ROWS[self.EU]), opt_holder)
        assert r.action == "fill" and says(r.value, "No")

    def test_region_yes_no_reads_every_nationality(self, opt_holder):
        a = dc_replace(opt_holder, nationalities=("India", "France"))
        r = resolve(field_of(self.ROWS[self.EU]), a)
        assert r.action == "fill" and says(r.value, "Yes")

    def test_an_alias_answers_like_its_country(self, opt_holder):
        a = dc_replace(opt_holder, nationalities=("Czech Republic",))
        r = resolve(field_of(self.ROWS[self.EU]), a)
        assert says(r.value, "Yes")

    def test_country_specific_yes_no(self, opt_holder):
        row = self.ROWS["Are you currently an Indonesian citizen (WNI)?✱"]
        r = resolve(field_of(row), opt_holder)
        assert r.action == "fill" and says(r.value, "No")

    def test_never_guesses_an_unmapped_region(self, opt_holder):
        row = dict(self.ROWS[self.EU], label="Are you a citizen of a country in Oceania?")
        r = resolve(field_of(row), opt_holder)
        assert r.action == "park" and r.tier == "B0"

    def test_asia_does_not_match_inside_asia_pacific(self, opt_holder):
        row = dict(self.ROWS[self.EU], label="Are you a citizen of an Asia-Pacific country?")
        a = dc_replace(opt_holder, nationalities=("Australia",))
        assert resolve(field_of(row), a).action == "park"


class TestFollowup:
    ROWS = rows_for("followup")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_fills_the_country_followup_text(self, row, opt_holder):
        if row["options"] or not names_only_the_us(row):
            pytest.skip("a picker, or another country's question")
        r = resolve(field_of(row), opt_holder)
        assert r.action == "fill" and r.value == OPT.followup

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_parks_without_one(self, row, opt_holder):
        a = dc_replace(opt_holder, countries={"default": ABROAD,
                                              "United States": dc_replace(OPT, followup="")})
        r = resolve(field_of(row), a)
        assert r.action == ("park" if row["required"] else "skip")


class TestTheAnswerableFamilies:
    AUTHORIZED = rows_for("plain", "proof")
    SPONSORSHIP = rows_for("sponsorship")

    @pytest.mark.parametrize("row", AUTHORIZED, ids=ids(AUTHORIZED))
    def test_authorization_follows_the_named_country(self, row, opt_holder):
        r = resolve(field_of(row), opt_holder)
        from src.apply.answers import places_named
        either = " or " in row["label"] and "United States" in places_named(row["label"])
        assert r.action == "fill"
        assert says(r.value, "Yes" if names_only_the_us(row) or either else "No")

    @pytest.mark.parametrize("row", SPONSORSHIP, ids=ids(SPONSORSHIP))
    def test_sponsorship_is_yes_for_an_opt_holder(self, row, opt_holder):
        r = resolve(field_of(row), opt_holder)
        if offers(row, "Yes"):
            assert r.action == "fill" and says(r.value, "Yes")
        else:
            assert r.action == ("park" if row["required"] else "skip")

    def test_a_citizen_answers_the_two_families_oppositely(self, us_citizen):
        for row in self.SPONSORSHIP:
            if offers(row, "No") and names_only_the_us(row):
                assert says(resolve(field_of(row), us_citizen).value, "No"), row["label"]


class TestTheQualifiedFamily:
    ROWS = rows_for("qualified")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_an_opt_holder_is_not_unrestricted(self, row, opt_holder):
        r = resolve(field_of(row), opt_holder)
        if offers(row, "No"):
            assert r.action == "fill" and says(r.value, "No")
        else:
            assert r.action == ("park" if row["required"] else "skip")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_a_citizen_is(self, row, us_citizen):
        if offers(row, "Yes") and names_only_the_us(row):
            assert says(resolve(field_of(row), us_citizen).value, "Yes")


class TestStatusClaims:
    ROWS = rows_for(*STATUS_CLAIM)

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_parks_with_no_claims_and_no_label(self, row, opt_holder):
        a = dc_replace(opt_holder, claims={}, countries={
            "default": ABROAD, "United States": dc_replace(OPT, status_label="")})
        r = resolve(field_of(row), a)
        assert r.action == ("park" if row["required"] else "skip")
        assert r.tier == "B0"

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_only_ever_picks_a_claimed_option(self, row, opt_holder):
        r = resolve(field_of(row), opt_holder)
        if r.action != "fill":
            return
        values = r.value if isinstance(r.value, tuple) else (r.value,)
        for value in values:
            assert value.casefold() in CLAIMS or value == OPT.status_label, value

    def test_a_free_text_status_question_takes_the_status_label(self, opt_holder):
        text_rows = [r for r in self.ROWS if not r["options"]]
        assert text_rows
        for row in text_rows:
            r = resolve(field_of(row), opt_holder)
            assert r.action == "fill" and r.value == "F-1 STEM OPT"

    def test_an_authorization_claim_never_answers_a_sponsorship_question(self, opt_holder):
        row = {"label": "Will you now or in the future require visa sponsorship?",
               "required": True, "kind": "select", "multi": False,
               "options": ["I am authorized to work in the United States for any employer",
                           "I am authorized to work lawfully in the United States and "
                           "DO require future sponsorship."]}
        r = resolve(field_of(row), opt_holder)
        assert r.value == row["options"][1]


class TestNoRowEverStatesSomethingFalse:
    """An OPT holder is authorized today, not permanently, and will need
    sponsorship. No row may answer in a way that contradicts that."""

    @pytest.mark.parametrize("row", CORPUS, ids=ids(CORPUS))
    def test_no_permanence_claim_is_ever_asserted(self, row, opt_holder):
        r = resolve(field_of(row), opt_holder)
        if r.action != "fill":
            return
        value = r.value[0] if isinstance(r.value, tuple) else r.value
        text = str(value).casefold()
        for claim in ("will not require", "do not require", "without sponsorship",
                      "no time limitations", "permanently authorized",
                      "will not need sponsorship", "i am a u.s. person"):
            assert claim not in text, f"{row['label']!r} answered {value!r}"


class TestCountries:
    """The same person is authorized in one country and not another."""

    def field(self, label, options=("Yes", "No")):
        return MergedField(id="q", name="q", label=label, required=True, kind="select",
                           section="questions", multi=False, api_type="x",
                           options=tuple(MergedOption(value=o, label=o) for o in options))

    def test_an_eu_question_answers_from_the_default_row(self, opt_holder):
        assert says(resolve(self.field("Are you eligible to work in the EU?"),
                            opt_holder).value, "No")

    def test_a_country_with_no_row_is_default(self, opt_holder):
        r = resolve(self.field("Are you authorized to work in Luxembourg?"), opt_holder)
        assert says(r.value, "No")

    def test_sponsorship_reads_the_named_country(self, opt_holder):
        r = resolve(self.field("Will you require visa sponsorship to work in India?"),
                    opt_holder)
        assert says(r.value, "No")

    def test_the_job_country_answers_an_unnamed_country(self, opt_holder):
        a = dc_replace(opt_holder, job_country="Germany")
        r = resolve(self.field(
            "Are you authorized to work in the country where this role is located?"), a)
        assert says(r.value, "No")

    def test_either_country_is_enough_for_or(self, opt_holder):
        r = resolve(self.field("Are you authorized to work in the US or Canada?"), opt_holder)
        assert says(r.value, "Yes")

    def test_both_are_needed_for_and(self, opt_holder):
        r = resolve(self.field("Are you authorized to work in the US and the UK?"), opt_holder)
        assert says(r.value, "No")

    def test_latin_america_is_not_the_us(self, opt_holder):
        r = resolve(self.field("Are you authorized to work in Latin America?"), opt_holder)
        assert says(r.value, "No")

    def test_h1b_or_other_visa_is_a_sponsorship_question(self, opt_holder):
        r = resolve(self.field("Do you need an H1-B or other visa to work here?"), opt_holder)
        assert r.tier == "B0" and says(r.value, "Yes")

    def test_a_negated_sponsorship_question_never_answers(self, opt_holder):
        r = resolve(self.field("Do you not need sponsorship to work in the US?"), opt_holder)
        assert r.action == "park"

    def test_us_person_is_never_a_bank_question(self, opt_holder):
        r = resolve(self.field("Are you a U.S. Person?"), opt_holder)
        assert r.tier == "B0"


def test_an_unreadable_job_country_parks_instead_of_guessing_home(opt_holder):
    from src.apply.answers import UNKNOWN_COUNTRY
    f = MergedField(id="q", name="q", label="Are you authorized to work in the country "
                    "where this role is located?", required=True, kind="select",
                    section="questions", multi=False, api_type="x",
                    options=(MergedOption(value="Yes", label="Yes"),
                             MergedOption(value="No", label="No")))
    r = resolve(f, dc_replace(opt_holder, job_country=UNKNOWN_COUNTRY))
    assert r.action == "park" and "could not be read" in r.reason
    named = MergedField(**{**f.__dict__, "label": "Are you authorized to work in the US?"})
    assert resolve(named, dc_replace(opt_holder, job_country=UNKNOWN_COUNTRY)).value == "Yes"
