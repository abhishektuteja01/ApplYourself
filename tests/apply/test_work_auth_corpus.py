"""The work-authorization golden corpus: every real label, with its category.

`work_auth_labels.jsonl` is every distinct (label, options) pair that read as
work-authorization-shaped across a 237-board harvest — 176 groups, 248 field
instances, from Greenhouse, Lever and Ashby. The `category` column is judgment,
clustered once in a command session against that harvest (R7 keeps the
clustering out of `src/`); everything below is mechanical.

Two assertions per row, deliberately separate:

1. `classify_work_authorization` puts the label in its recorded category. Without
   this the category column would be documentation, and a row that reached the
   right answer through the wrong category would pass silently.
2. `resolve` does what that category's policy says, given a config.

The corpus is selected by a net deliberately WIDER than
`WORK_AUTHORIZATION_DOMAIN` — filtering by the runtime regex could only ever
confirm what the code already sees. That is why `not_work_auth` exists as a
category: seven rows mention sponsorship or eligibility and are not
work-authorization questions at all (an age check, a security-clearance
question, a relocation-cities checkbox). They must keep falling through to tier
C, where the `/apply` session answers them as ordinary questions. Pulling one
into the work-auth resolver parks it at B0, and a B0 park makes the whole role
manual-apply — a regression that looks like a fix.
"""
from __future__ import annotations

import json
from dataclasses import replace as dc_replace

import pytest

from src.apply.answers import (
    WORK_AUTH_CATEGORIES,
    classify_work_authorization,
    load_answers,
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

# Categories whose answer is a statement only the user can make.
STATUS_CLAIM = ("status_disclosure", "compound_option")
# Categories that park no matter how the config is set.
ALWAYS_PARK = ("export_control", "followup", "names_other_country", "nationality",
               "alternation")


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
    """Whether a resolved value is this polarity, as the board spells it.

    `_pick_option` deliberately returns the option's own text so `fill.py` can
    select it, and real boards render "Yes ", "Yes." and "Yes" for the same
    thing. Comparing to a bare "Yes" would fail on the board, not on the answer.
    """
    got = value[0] if isinstance(value, tuple) else value
    return str(got).strip().rstrip(".").casefold() == word.casefold()


def offers(row, word: str) -> bool:
    """Whether a bare "Yes"/"No" for this polarity is on offer.

    A row whose option list splits the polarity across qualified sentences
    ("Yes, I will need to transfer an existing work visa", "Yes, I will require a
    new visa sponsorship") cannot be answered from a Yes/No, so the resolver
    falls through to the user's own candidate list.
    """
    if not row["options"]:
        return True                      # a text box takes the word itself
    return any(o.strip().rstrip(".").casefold() == word.casefold()
               for o in row["options"])


@pytest.fixture
def configured(answers):
    """The config this corpus was sized against: a time-limited status that has
    stated its scope answer and its own status wording."""
    return dc_replace(
        answers,
        scope_qualified_answer="no",
        status_label="F-1 STEM OPT",
        status_option_candidates=(
            # F-1 before STEM OPT on purpose: "F1" is true whether or not the
            # STEM extension has started, while "STEM OPT" asserts it has.
            "F-1", "F1", "STEM OPT",
            "Yes, and I am currently in F-1 status (if you currently have OPT or "
            "STEM-EXT OPT or CPT, you will select this answer.)",
            "I am authorized to work lawfully in the United States and DO require "
            "future sponsorship.",
        ),
    )


class TestTheCorpusIsWellFormed:
    def test_every_row_names_a_known_category(self):
        unknown = {r["category"] for r in CORPUS} - set(WORK_AUTH_CATEGORIES)
        assert not unknown

    def test_the_corpus_is_the_size_it_claims(self):
        assert len(CORPUS) == 176
        assert sum(r["boards"] for r in CORPUS) == 248

    def test_rows_are_unique_on_label_and_options(self):
        keys = [(r["norm_label"], tuple(r["options"])) for r in CORPUS]
        assert len(keys) == len(set(keys))

    def test_alternation_is_absent_and_that_is_recorded(self):
        """No board in this harvest offered the qualifier as an alternative, but
        `answers.py` implements the case and `test_answers.py` covers it with
        three live labels from the earlier census. Asserted so a future harvest
        that does contain one has somewhere obvious to land."""
        assert not rows_for("alternation")


class TestClassification:
    @pytest.mark.parametrize("row", CORPUS, ids=ids(CORPUS))
    def test_the_label_lands_in_its_recorded_category(self, row):
        got = classify_work_authorization(row["label"], tuple(row["options"]))
        assert got == row["category"], (
            f"classified {got!r}, corpus says {row['category']!r}"
        )


class TestNotWorkAuthStaysOutOfTheResolver:
    """The regression the widened domain regex could cause. Each of these
    mentions sponsorship or eligibility in passing; none is a work-authorization
    question. Tier C means the `/apply` session handles it; tier B0 would park
    the role for manual application."""

    ROWS = rows_for("not_work_auth")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_resolves_at_tier_c_not_b0(self, row, configured):
        r = resolve(field_of(row), configured)
        # "not B0" rather than "== C": with a fuller rules[] set some of these are
        # legitimately answered by a Tier B rule, which is fine. What must never
        # happen is landing in the work-auth resolver, whose parks are B0 and
        # therefore unreachable from the /apply session.
        assert r.tier != "B0", "a B0 park would make this role manual-apply"


class TestCategoriesThatParkWhateverTheConfigSays:
    ROWS = rows_for(*ALWAYS_PARK)

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_parks_even_fully_configured(self, row, configured):
        r = resolve(field_of(row), configured)
        expected = "park" if row["required"] else "skip"
        assert r.action == expected
        assert r.tier == "B0"


class TestTheAnswerableFamilies:
    """`plain` and `proof` answer from `status`; `sponsorship` from the same
    status read the other way. A row whose option list cannot express the bare
    polarity falls through to the user's candidate list instead."""

    AUTHORIZED = rows_for("plain", "proof")
    SPONSORSHIP = rows_for("sponsorship")

    @pytest.mark.parametrize("row", AUTHORIZED, ids=ids(AUTHORIZED))
    def test_a_plain_authorization_question_answers_yes(self, row, answers):
        # Unconfigured on purpose: these must resolve from `status` alone, with
        # no scope answer and no status label set.
        r = resolve(field_of(row), answers)
        assert offers(row, "Yes"), "a plain row should offer a bare Yes"
        assert r.action == "fill"
        assert says(r.value, "Yes")

    @pytest.mark.parametrize("row", SPONSORSHIP, ids=ids(SPONSORSHIP))
    def test_a_sponsorship_question_answers_yes_for_a_time_limited_status(
        self, row, answers
    ):
        r = resolve(field_of(row), answers)
        if offers(row, "Yes"):
            assert r.action == "fill"
            assert says(r.value, "Yes")
        else:
            # The "Yes" branch is split across qualified sentences, so nothing
            # but the user's own list can pick one.
            assert r.action == ("park" if row["required"] else "skip")

    def test_a_citizen_answers_the_two_families_oppositely(self, tmp_path, answers):
        """The property one keyword rule cannot express, over the whole corpus:
        authorized yes, sponsorship no."""
        citizen = dc_replace(answers, status="citizen_or_pr")
        for row in self.SPONSORSHIP:
            if not offers(row, "No"):
                continue
            r = resolve(field_of(row), citizen)
            assert says(r.value, "No"), row["label"]


class TestTheQualifiedFamily:
    """The never-guess guard, over every real qualified label. `status` alone
    cannot say whether you are *permanently* authorized, so the default parks and
    only the user's own `scope_qualified_answer` resolves it."""

    ROWS = rows_for("qualified")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_parks_by_default(self, row, answers):
        assert answers.scope_qualified_answer == "park"
        r = resolve(field_of(row), answers)
        assert r.action == ("park" if row["required"] else "skip")
        assert r.tier == "B0"

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_answers_no_once_configured(self, row, configured):
        r = resolve(field_of(row), configured)
        if offers(row, "No"):
            assert r.action == "fill"
            assert says(r.value, "No")
        else:
            assert r.action == ("park" if row["required"] else "skip")

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_a_citizen_answers_yes_without_consulting_the_setting(self, row, answers):
        """For a citizen or permanent resident, permanence and freedom from
        sponsorship are knowable and both Yes."""
        citizen = dc_replace(answers, status="citizen_or_pr")
        r = resolve(field_of(citizen_row := row), citizen)
        if offers(citizen_row, "Yes"):
            assert says(r.value, "Yes")


class TestStatusClaims:
    """"Which status do you hold" and "pick the option that is true of you" are
    the same question in two widgets. Neither is derivable from `status`, which
    groups OPT, H-1B and TN together, so both read the user's own words."""

    ROWS = rows_for(*STATUS_CLAIM)

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_parks_when_nothing_is_configured(self, row, answers):
        assert answers.status_label == ""
        assert answers.status_option_candidates == ()
        r = resolve(field_of(row), answers)
        assert r.action == ("park" if row["required"] else "skip")
        assert r.tier == "B0"

    @pytest.mark.parametrize("row", ROWS, ids=ids(ROWS))
    def test_it_never_invents_an_answer_when_configured(self, row, configured):
        """Whatever it does, it either picks an option the user listed verbatim
        or parks. It must never settle on an option merely because the board
        offered it."""
        r = resolve(field_of(row), configured)
        if r.action != "fill":
            return
        value = r.value[0] if isinstance(r.value, tuple) else r.value
        listed = {c.casefold() for c in configured.status_option_candidates}
        assert (value.casefold() in listed
                or value == configured.status_label), value

    def test_a_free_text_status_question_takes_the_configured_wording(self, configured):
        text_rows = [r for r in self.ROWS if not r["options"]]
        assert text_rows, "the corpus should hold at least one free-text one"
        for row in text_rows:
            r = resolve(field_of(row), configured)
            assert r.action == "fill"
            assert r.value == "F-1 STEM OPT"


class TestNoRowEverStatesSomethingFalse:
    """The property that matters more than coverage. A `time_limited` status is
    authorized today, is not permanently authorized, and will need sponsorship.
    Across the whole corpus and both scope settings, no row may answer in a way
    that contradicts that.
    """

    @pytest.mark.parametrize("row", CORPUS, ids=ids(CORPUS))
    @pytest.mark.parametrize("scope", ["park", "no"])
    @pytest.mark.parametrize("claims", ["unset", "configured"])
    def test_no_permanence_claim_is_ever_asserted(self, row, scope, claims,
                                                  answers, configured):
        """Runs with `status_option_candidates` populated as well as empty. That
        path is the only one that can emit a long claim-bearing string, so it is
        the one that can violate this — the day someone pastes "Yes, and I will
        not require employer support..." into the list because a board offered
        it, this is what catches it."""
        base = configured if claims == "configured" else answers
        a = dc_replace(base, scope_qualified_answer=scope)
        r = resolve(field_of(row), a)
        if r.action != "fill":
            return
        value = r.value[0] if isinstance(r.value, tuple) else r.value
        text = str(value).casefold()
        # An option the resolver picked must not itself assert permanence or
        # freedom from future sponsorship.
        for claim in ("will not require", "do not require", "without sponsorship",
                      "no time limitations", "permanently authorized",
                      "will not need sponsorship", "i am a u.s. person"):
            assert claim not in text, f"{row['label']!r} answered {value!r}"
