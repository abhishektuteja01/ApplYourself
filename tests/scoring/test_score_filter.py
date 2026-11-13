"""The hard rules /score applies before any judge (src/applyourself/score_filter.py)."""

from __future__ import annotations

import pytest

from applyourself import score_filter as sf
from applyourself.shared import paths

REAL_SCORING = paths.example("scoring.example.yaml")
EXAMPLE_BANK = paths.example("application_answers.example.yaml")


@pytest.fixture
def scfg():
    return sf.load_scoring(REAL_SCORING)


class TestLoadScoring:
    def test_committed_default_loads(self, scfg):
        assert scfg.review_min <= scfg.tailor_min
        assert "green card required" in scfg.ineligible

    def test_the_needs_come_from_the_bank(self, scfg):
        assert scfg.sponsorship_need == "later" and scfg.default_need == "now"

    @pytest.mark.parametrize(
        "old, new, error",
        [
            ("review: 50", "review: 70", "cutoffs: .*must rise"),
            ("cutoffs:", "action:", "'action' \\(moved to cutoffs.review and cutoffs.tailor\\)"),
        ],
    )
    def test_cutoffs_out_of_order_or_under_the_old_key_fail(self, tmp_path, old, new, error):
        text = REAL_SCORING.read_text(encoding="utf-8").replace(old, new)
        (tmp_path / "s.yaml").write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=error):
            sf.load_scoring(tmp_path / "s.yaml", EXAMPLE_BANK)


class TestFindPhrase:
    @pytest.mark.parametrize(
        "jd",
        [
            "Applicants must be a U.S. citizen.",
            "must be a United States citizen",
            "MUST BE A US CITIZEN",
            "must be a **U.S.** citizen",
        ],
    )
    def test_us_spellings_match(self, jd):
        assert sf.find_phrase(("must be a us citizen",), jd) == "must be a us citizen"

    @pytest.mark.parametrize(
        "phrase, jd",
        [
            ("concur", "work concurrently with the team"),
            ("ts/sci", "a degree in arts/sciences"),
            ("solution architect", "own the solution architecture"),
        ],
    )
    def test_whole_words_only(self, phrase, jd):
        assert sf.find_phrase((phrase,), jd) is None

    def test_no_phrases_no_match(self):
        assert sf.find_phrase((), "anything") is None


class TestYearsRequired:
    @pytest.mark.parametrize(
        "jd, years",
        [
            ("5+ years of experience in Python", 5),
            ("3-6 years of relevant experience", 3),
            ("3 to 5 years of experience", 3),
            ("3–4 years experience", 3),
            ("7+ years building production software", 0),
            ("2 years of hands-on software experience", 2),
            ("our 150 year legacy", 0),
            ("with over 100 years of experience serving clients", 0),
            ("a company with more than 60 years of experience", 0),
            ("education – 15 years experience", 0),
            ("34 years of experience as a systems analyst", 0),
            ("the program runs for 4 years", 0),
            ("", 0),
            (None, 0),
        ],
    )
    def test_reads_the_minimum(self, jd, years):
        assert sf.years_required(jd) == years

    def test_preferred_section_ignored(self):
        jd = "Requirements:\n- 3+ years of experience\n\nPreferred qualifications\n- 8+ years of experience"
        assert sf.years_required(jd) == 3

    def test_highest_required_wins(self):
        assert (
            sf.years_required("2+ years of Python experience and 4+ years of SQL experience") == 4
        )

    def test_inline_preferred_heading_ignored(self):
        jd = (
            "Basic Qualifications: At least 4 years of experience in software engineering. "
            "Preferred Qualifications: Master's degree, 9+ years of experience coding"
        )
        assert sf.years_required(jd) == 4

    DEGREE_PATHS = (
        "Bachelor's degree and 7+ years of related experience, "
        "or a Master's degree and 4+ years of related experience"
    )

    @pytest.mark.parametrize("degree, years", [(3, 4), (4, 4), (2, 7), (None, 7)])
    def test_degree_paths_take_the_open_one(self, degree, years):
        assert sf.years_required(self.DEGREE_PATHS, degree) == years

    def test_no_open_path_takes_the_lowest(self):
        jd = "Master's degree and 5 years of experience, or PhD and 2 years of experience"
        assert sf.years_required(jd, 2) == 2

    def test_lower_degree_path_open_to_a_higher_degree(self):
        jd = (
            "Bachelor's degree and 5-7 years of job-related experience or "
            "high school diploma/GED and 10 years of the same job-related experience"
        )
        assert sf.years_required(jd, 3) == 5

    def test_or_without_degrees_is_not_a_path(self):
        jd = "3+ years of Python experience or 6+ years of Java experience"
        assert sf.years_required(jd, 3) == 6

    @pytest.mark.parametrize(
        "text, rank",
        [
            ("Master's Degree", 3),
            ("Bachelor of Technology", 2),
            ("PhD", 4),
            ("Associate's", 1),
            ("High School Diploma", 0),
            ("", None),
            (None, None),
        ],
    )
    def test_degree_rank(self, text, rank):
        assert sf.degree_rank(text) == rank

    def test_bank_degree_reaches_the_config(self, scfg):
        assert scfg.degree_rank == 3


class TestTrimJd:
    def test_load_trim_reads_the_trim_block_alone(self, scfg, tmp_path):
        assert sf.load_trim(REAL_SCORING) == (scfg.trim_headings, scfg.keep_words)
        assert sf.load_trim(tmp_path / "missing.yaml") == ((), ())

    def test_boilerplate_section_cut_until_next_heading(self, scfg):
        jd = (
            "## About the role\n\nBuild LLM agents.\n\n## Benefits\n\nDental.\n\n"
            "Free lunch.\n\n## Requirements\n\n3+ years of Python."
        )
        out = sf.trim_jd(jd, scfg)
        assert "Build LLM agents." in out and "3+ years of Python." in out
        assert "Dental." not in out and "Free lunch." not in out

    def test_keep_word_paragraph_survives_inside_boilerplate(self, scfg):
        jd = (
            "Build agents.\n\n## Equal Opportunity\n\nWe are an EEO employer.\n\n"
            "We do not offer visa sponsorship for this role."
        )
        out = sf.trim_jd(jd, scfg)
        assert "visa sponsorship" in out
        assert "EEO employer" not in out

    def test_heading_glued_to_its_body_still_cuts(self, scfg):
        jd = "Build agents.\nBenefits:\n- Dental\n- Gym\nRequirements:\n- 3+ years of Python"
        out = sf.trim_jd(jd, scfg)
        assert "Dental" not in out and "Gym" not in out
        assert "Build agents." in out and "3+ years of Python" in out

    def test_bullets_are_never_headings(self, scfg):
        jd = "## Benefits\n\n- Dental\n\n- Gym\n\n## Role\n\n- Ship LLM features"
        out = sf.trim_jd(jd, scfg)
        assert "Dental" not in out and "Gym" not in out and "Ship LLM features" in out

    def test_no_headings_keeps_everything(self, scfg):
        jd = "Line one of the role.\n\nLine two of the role."
        assert sf.trim_jd(jd, scfg) == jd


def test_wrap_leaves_no_long_line():
    text = "word " * 1000
    assert max(len(line) for line in sf.wrap(text).splitlines()) <= 100


class TestDropReason:
    def _row(self, **kw):
        return {
            "job_id": "aaaaaaaa",
            "vertical": "example_primary",
            "jd_text": "Build widgets.",
            **kw,
        }

    def test_no_lane(self, cfg, scfg):
        assert sf.drop_reason(self._row(vertical=""), scfg)[0] == "no-lane"

    def test_ineligible(self, cfg, scfg):
        code, detail = sf.drop_reason(self._row(jd_text="Must be a U.S. citizen."), scfg)
        assert (code, detail) == ("ineligible", "must be a us citizen")

    @pytest.mark.parametrize(
        "jd, dropped",
        [
            ("This position does require security clearance.", True),
            ("The role requires a security clearance.", True),
            ("This role may require a security clearance.", False),
        ],
    )
    def test_clearance_wording(self, cfg, scfg, jd, dropped):
        assert (sf.drop_reason(self._row(jd_text=jd), scfg) is not None) is dropped

    def test_no_sponsorship_only_when_needed(self, cfg, scfg):
        row = self._row(jd_text="We will not sponsor visas.")
        assert sf.drop_reason(row, scfg) is None
        needs = sf.ScoringConfig(**{**scfg.__dict__, "sponsorship_need": "now"})
        assert sf.drop_reason(row, needs)[0] == "ineligible"

    def test_ineligible_in_title_only(self, cfg, scfg):
        row = self._row(title="Widget Consultant - US Citizens Only")
        assert sf.drop_reason(row, scfg)[0] == "ineligible"

    def test_lane_phrases_and_years_go_to_the_judge(self, cfg, scfg):
        jd = "Uses the rival widget suite. 9+ years of experience."
        assert sf.drop_reason(self._row(jd_text=jd), scfg) is None


def test_retention_never_expires_a_role_above_its_lane_line(cfg, tmp_path):
    import dataclasses

    import yaml

    from applyourself.shared import verticals

    def cuts(**extra):
        s = tmp_path / "s.yaml"
        s.write_text(
            yaml.safe_dump({"cutoffs": {"review": 50, "tailor": 60, **extra}}), encoding="utf-8"
        )
        return sf.lane_cutoffs(s), sf.retention_cutoff(s)

    lines = {"example_primary": 80, "example_secondary": 78, "example_tertiary": 80}
    assert cuts() == (lines, 78)
    assert cuts(keep_long=75) == (lines, 75)
    assert cuts(keep_long=90, sheet=95) == (lines, 78)  # neither old key moves a lane line
    plain = {
        n: dataclasses.replace(v, score=dataclasses.replace(v.score, cutoff=None))
        for n, v in cfg.verticals.items()
    }
    verticals.set_config(dataclasses.replace(cfg, verticals=plain))
    assert cuts(sheet=85)[0] == dict.fromkeys(lines, 85)  # an unmigrated profile
    assert cuts()[0] == dict.fromkeys(lines, 76)  # scoring_defaults.yaml


def _need(scfg, need):
    return sf.ScoringConfig(
        **{
            **scfg.__dict__,
            "sponsorship_need": need,
            "country_needs": (("United States", need), ("India", need)),
            "default_need": "now",
        }
    )


class TestSponsorshipLabel:
    @pytest.mark.parametrize(
        "fact, never, later, now",
        [
            ("offers", "sponsors", "sponsors", "sponsors"),
            ("silent", "unknown", "unknown", "unknown"),
            ("status_bar", "ineligible", "ineligible", "ineligible"),
            ("no_sponsorship", "opt_ok", "opt_ok", "ineligible"),
            ("no_future_sponsorship", "opt_ok", "ineligible", "ineligible"),
            ("no_opt_support", "opt_ok", "ineligible", "opt_ok"),
            ("right_to_work", "unknown", "unknown", "ineligible"),
        ],
    )
    def test_fact_times_need(self, scfg, fact, never, later, now):
        got = [
            sf.sponsorship_label([fact], "United States", _need(scfg, n))
            for n in ("never", "later", "now")
        ]
        assert got == [never, later, now]

    def test_visa_needed_country_always_needs_sponsorship(self, scfg):
        cfg = _need(scfg, "never")
        assert sf.sponsorship_label(["right_to_work"], "Germany", cfg) == "ineligible"
        assert sf.sponsorship_label(["no_sponsorship"], "Germany", cfg) == "ineligible"
        assert sf.sponsorship_label(["offers"], "Germany", cfg) == "sponsors"
        assert sf.sponsorship_label(["right_to_work"], "India", cfg) == "unknown"
        assert sf.sponsorship_label(["right_to_work"], "", cfg) == "unknown"
        assert sf.sponsorship_label(["right_to_work"], "Germany; USA", cfg) == "unknown"

    def test_each_country_has_its_own_need(self, scfg):
        cfg = sf.ScoringConfig(
            **{
                **scfg.__dict__,
                "sponsorship_need": "later",
                "country_needs": (("United States", "later"), ("India", "never")),
                "default_need": "now",
            }
        )
        assert sf.sponsorship_label(["no_future_sponsorship"], "India", cfg) == "opt_ok"
        assert sf.sponsorship_label(["no_future_sponsorship"], "United States", cfg) == "ineligible"

    @pytest.mark.parametrize(
        "facts, label",
        [
            (["no_sponsorship", "no_opt_support"], "ineligible"),
            (["no_sponsorship"], "opt_ok"),
            (["offers", "no_sponsorship"], "sponsors"),
            (["offers", "status_bar"], "ineligible"),
            ([], "unknown"),
        ],
    )
    def test_strictest_statement_wins(self, scfg, facts, label):
        assert sf.sponsorship_label(facts, "United States", _need(scfg, "later")) == label

    def test_no_sponsorship_when_later_follows_the_scoring_key(self, scfg, tmp_path):
        text = REAL_SCORING.read_text(encoding="utf-8")
        (tmp_path / "s.yaml").write_text(
            text + "\nno_sponsorship_when_later: ineligible\n", encoding="utf-8"
        )
        cfg = sf.load_scoring(tmp_path / "s.yaml", EXAMPLE_BANK)
        got = [
            sf.sponsorship_label(["no_sponsorship"], "United States", _need(cfg, n))
            for n in ("never", "later", "now")
        ]
        assert got == ["opt_ok", "ineligible", "ineligible"]

    def test_offer_cancels_right_to_work_abroad(self, scfg):
        assert (
            sf.sponsorship_label(["right_to_work", "offers"], "Germany", _need(scfg, "never"))
            == "sponsors"
        )


class TestCountryAllowed:
    RULE = sf.CountryRule(home=frozenset({"us"}), abroad={"germany": frozenset({"lane_a"})})

    @pytest.mark.parametrize(
        "country, lane, ok",
        [
            ("United States", "lane_b", True),
            ("USA", "lane_b", True),
            ("U.S.", "lane_b", True),
            ("", "lane_b", True),
            ("Remote", "lane_b", True),
            ("Germany", "lane_a", True),
            ("Germany", "lane_b", False),
            ("Switzerland", "lane_a", False),
            ("Switzerland; United States", "lane_b", True),
        ],
    )
    def test_rule(self, country, lane, ok):
        assert sf.country_allowed(country, lane, self.RULE) is ok

    def test_from_discovery(self):
        from applyourself.discovery.config import DiscoveryConfig, LocationAllowlist, SourceConfig

        d = DiscoveryConfig(
            location_allowlist=LocationAllowlist(countries=["United States", "Germany"]),
            sources={
                "linkedin": SourceConfig(
                    True, 3.0, abroad_countries=["Germany"], abroad_verticals=["lane_a"]
                )
            },
        )
        rule = sf.CountryRule.from_discovery(d)
        assert rule.home == {"us"} and rule.abroad == {"germany": {"lane_a"}}
