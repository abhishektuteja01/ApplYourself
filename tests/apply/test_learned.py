"""The learned answer store — `src/apply/learned.py`.

The store exists so `profile/application_answers.yaml` can stay small and
hand-written while the accumulated knowledge of how 200-odd boards word their
questions grows somewhere nobody has to read. These tests pin the two
properties that makes safe: everything learned is validated exactly as a
hand-written rule is, and nothing learned can override something stated.
"""
from __future__ import annotations

import json

import pytest

from src.apply import learned as store
from src.apply.answers import AnswersError, load_answers, resolve
from src.apply.reconcile import MergedField, MergedOption

from .conftest import FIXTURES

CONFIG = FIXTURES / "application_answers.yaml"
PREFS = FIXTURES / "preferences_time_limited.md"


def write_store(tmp_path, *records):
    path = tmp_path / ".apply_learned.jsonl"
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return path


def field(**kw) -> MergedField:
    base = dict(id="question_1", name="question_1", label="", required=False,
                kind="text", section="questions", multi=False, options=())
    options = kw.pop("options", None)
    if options is not None:
        kw["options"] = tuple(MergedOption(label=o) for o in options)
    base.update(kw)
    return MergedField(**base)


def load(path=None):
    return load_answers(CONFIG, PREFS, learned_path=path)


class TestReadingTheStore:
    def test_an_absent_store_is_a_no_op(self, tmp_path):
        answers = load(tmp_path / "nothing.jsonl")
        assert answers.learned_warnings == ()
        assert answers.vetoes == ()

    def test_a_malformed_line_is_skipped_and_named(self, tmp_path):
        path = tmp_path / ".apply_learned.jsonl"
        path.write_text('{"kind": "wording"\nnot json at all\n', encoding="utf-8")
        records, warnings = store.read_records(path)
        assert records == []
        assert len(warnings) == 2
        assert "not JSON" in warnings[0]

    def test_one_bad_line_never_blocks_a_run(self, tmp_path):
        """The config is fail-closed because a wrong answer goes out under the
        user's name. The store is fail-open because it is a convenience, and a
        stale line that could block every future submission is worse than a
        line that does nothing."""
        path = tmp_path / ".apply_learned.jsonl"
        path.write_text("garbage\n", encoding="utf-8")
        answers = load(path)          # must not raise
        assert answers.learned_warnings

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        path = tmp_path / ".apply_learned.jsonl"
        path.write_text("# a note\n\n", encoding="utf-8")
        assert store.read_records(path) == ([], [])

    def test_an_unknown_kind_is_skipped(self, tmp_path):
        path = write_store(tmp_path, {"kind": "telepathy", "wording": "x"})
        records, warnings = store.read_records(path)
        assert records == []
        assert "unknown kind" in warnings[0]


class TestLearnedWordings:
    def test_a_wording_joins_its_group(self, tmp_path):
        path = write_store(tmp_path, {
            "kind": "wording", "group": "how did you hear",
            "wording": "where did you first come across this opening",
        })
        answers = load(path)
        rule = next(r for r in answers.rules if r.group == "how did you hear")
        assert "where did you first come across this opening" in rule.match

    def test_it_joins_at_the_end_so_a_stated_keyword_wins(self, tmp_path):
        path = write_store(tmp_path, {
            "kind": "wording", "group": "how did you hear", "wording": "learned of us",
        })
        rule = next(r for r in load(path).rules if r.group == "how did you hear")
        assert rule.match[-1] == "learned of us"

    def test_a_duplicate_wording_is_not_added_twice(self, tmp_path):
        path = write_store(tmp_path,
                            {"kind": "wording", "group": "how did you hear",
                             "wording": "learned of us"},
                            {"kind": "wording", "group": "how did you hear",
                             "wording": "Learned Of Us"})
        rule = next(r for r in load(path).rules if r.group == "how did you hear")
        assert rule.match.count("learned of us") == 1

    def test_a_record_naming_no_group_warns_rather_than_failing(self, tmp_path):
        path = write_store(tmp_path, {
            "kind": "wording", "group": "no such group", "wording": "anything",
        })
        answers = load(path)
        assert any("no rule owns group" in w for w in answers.learned_warnings)

    def test_a_learned_wording_gets_the_same_validation_as_a_written_one(self, tmp_path):
        """The whole reason folding happens before `_parse_rules`: a learned
        keyword that would shadow another rule must be refused, not silently
        change which rule answers a question."""
        path = write_store(tmp_path, {
            "kind": "wording", "group": "how did you hear", "wording": "portfolio site",
        })
        with pytest.raises(AnswersError, match="overlaps"):
            load(path)

    def test_a_learned_work_authorization_keyword_is_refused(self, tmp_path):
        path = write_store(tmp_path, {
            "kind": "answer", "wording": "will you require sponsorship",
            "answer": ["No"],
        })
        with pytest.raises(AnswersError, match="work-authorization keyword"):
            load(path)


class TestLearnedOptions:
    def test_an_option_spelling_joins_the_candidate_list_last(self, tmp_path):
        path = write_store(tmp_path, {
            "kind": "option", "group": "how did you hear",
            "option": "Acme Careers Site", "mode": "contains",
        })
        rule = next(r for r in load(path).rules if r.group == "how did you hear")
        assert rule.answers[-1] == "Acme Careers Site"
        assert rule.mode == "contains"

    def test_a_learned_option_resolves_the_park_it_was_learned_from(self, tmp_path):
        """End to end: the park this store exists to stop happening twice."""
        heard = field(label="How did you hear about this job?", required=True,
                       kind="select", options=["Acme Careers Site", "LinkedIn"])
        assert resolve(heard, load()).action == "park"

        path = write_store(tmp_path, {
            "kind": "option", "group": "how did you hear",
            "option": "careers site", "mode": "contains",
        })
        resolution = resolve(heard, load(path))
        assert resolution.action == "fill"
        assert resolution.value == "Acme Careers Site"


class TestVetoes:
    """The one record kind that removes an answer. A keyword can be right about
    one label and backwards about another, and no edit to the keyword fixes one
    without breaking the other."""

    RULE = {"kind": "answer", "wording": "ai policy", "answer": ["Yes"],
            "mode": "contains"}

    def test_a_veto_stops_its_group_matching_that_label(self, tmp_path):
        acknowledged = field(label="Acknowledge our AI policy",
                              required=True, kind="select", options=["Yes", "No"])
        asked = field(label="Did you use AI to prepare this application? See our "
                             "AI policy.",
                       required=True, kind="select", options=["Yes", "No"])

        # One keyword, both labels, the same answer -- and it is a claim the
        # user never made on the second.
        learned_only = load(write_store(tmp_path, self.RULE))
        assert resolve(acknowledged, learned_only).value == "Yes"
        assert resolve(asked, learned_only).value == "Yes"

        path = write_store(tmp_path, self.RULE, {
            "kind": "veto", "group": "ai policy",
            "wording": "did you use ai to prepare this application see our ai policy",
        })
        vetoed = load(path)
        assert resolve(acknowledged, vetoed).value == "Yes", "the right label still resolves"
        after = resolve(asked, vetoed)
        assert after.tier == "C", "the wrong one falls through to judgment"
        assert after.action == "park"

    def test_a_veto_is_matched_on_the_whole_label_not_a_substring(self, tmp_path):
        path = write_store(tmp_path, self.RULE, {
            "kind": "veto", "group": "ai policy", "wording": "ai policy",
        })
        # The veto names a label nobody asks verbatim, so a real label with
        # that text inside it still resolves.
        still = field(label="Acknowledge our AI policy", required=True,
                       kind="select", options=["Yes", "No"])
        assert resolve(still, load(path)).value == "Yes"

    def test_a_veto_missing_a_half_is_skipped(self, tmp_path):
        path = write_store(tmp_path, {"kind": "veto", "group": "ai policy"})
        answers = load(path)
        assert answers.vetoes == ()
        assert any("needs both" in w for w in answers.learned_warnings)


class TestStandaloneAnswers:
    def test_a_new_question_becomes_a_rule_at_the_end_of_the_chain(self, tmp_path):
        path = write_store(tmp_path, {
            "kind": "answer", "wording": "have you deployed production grade applications",
            "answer": ["Yes"],
        })
        answers = load(path)
        assert answers.rules[-1].match == ("have you deployed production grade applications",)
        question = field(label="Have you deployed production-grade applications?",
                          required=True)
        assert resolve(question, answers).value == "Yes"

    def test_it_cannot_shadow_a_configured_rule(self, tmp_path):
        """Appended last, so a configured rule always matches first."""
        path = write_store(tmp_path, {
            "kind": "answer", "wording": "portfolio", "answer": ["shadowed"],
        })
        # `portfolio` is configured; a learned rule claiming the same keyword
        # would shadow it, and the overlap check refuses it outright.
        with pytest.raises(AnswersError, match="overlap|both match"):
            load(path)


class TestWriting:
    def test_append_writes_one_line_per_record(self, tmp_path):
        path = tmp_path / ".apply_learned.jsonl"
        store.append(store.new_record("wording", group="g", wording="a wording"), path)
        store.append(store.new_record("wording", group="g", wording="another"), path)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["wording"] == "a wording"

    def test_every_record_is_stamped_with_a_date(self):
        assert store.new_record("wording", group="g", wording="w").learned

    def test_a_wording_under_the_length_floor_is_refused(self):
        with pytest.raises(store.LearnedError, match="shorter than"):
            store.validate(store.new_record("wording", group="g", wording="ai"))

    def test_a_record_missing_its_group_is_refused(self):
        with pytest.raises(store.LearnedError, match="needs a group"):
            store.validate(store.new_record("wording", wording="a wording"))

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(store.LearnedError, match="not 'exact' or 'contains'"):
            store.validate(store.new_record("option", group="g", option="x", mode="fuzzy"))

    def test_the_group_of_a_rule_is_its_first_keyword(self):
        assert store.group_of({"match": ["how did you hear", "become aware"]}) == \
            "how did you hear"
        assert store.group_of({"exact": ["State"]}) == "state"
        assert store.group_of({"answer": ["x"]}) == ""


class TestStoreOrderIndependence:
    """An append-only file cannot promise what order its lines arrive in, so a
    record's meaning must not depend on it."""

    def test_a_wording_can_join_a_group_a_later_line_creates(self, tmp_path):
        path = write_store(
            tmp_path,
            {"kind": "wording", "group": "office hub", "wording": "which hub would you work from"},
            {"kind": "answer", "wording": "office hub", "answer": ["Yes"]},
        )
        answers = load(path)
        rule = next(r for r in answers.rules if r.group == "office hub")
        assert "which hub would you work from" in rule.match
        assert answers.learned_warnings == ()

    def test_an_option_can_join_a_group_a_later_line_creates(self, tmp_path):
        path = write_store(
            tmp_path,
            {"kind": "option", "group": "office hub", "option": "Yes, that works"},
            {"kind": "answer", "wording": "office hub", "answer": ["Yes"]},
        )
        rule = next(r for r in load(path).rules if r.group == "office hub")
        assert rule.answers == ("Yes", "Yes, that works")


class TestTheStoreIsNeverReadByAccident:
    def test_the_suite_cannot_reach_the_real_store(self):
        """`tests/conftest.py::_isolate_learned_store` repoints the default
        path. Without it, a record learned from one live board would change
        what the fixture boards resolve to, and the failure would surface in
        whichever unrelated test shared a keyword — which is exactly how this
        was found."""
        assert not store.DEFAULT_LEARNED_PATH.exists()

    def test_false_skips_the_store_entirely(self, tmp_path):
        path = write_store(tmp_path, {
            "kind": "wording", "group": "how did you hear", "wording": "a learned wording",
        })
        opted_out = load_answers(CONFIG, PREFS, learned_path=False)
        rule = next(r for r in opted_out.rules if r.group == "how did you hear")
        assert "a learned wording" not in rule.match
        # ...and the same call with the path does read it.
        rule = next(r for r in load(path).rules if r.group == "how did you hear")
        assert "a learned wording" in rule.match
