"""src/verticals.py loader tests: fixture happy path, byte-parity anchors
(stamps/reasoning texts must equal the pre-refactor literals so
scored.parquet rows stay identical), validation failures, and the committed
example template."""

from pathlib import Path

import pytest
import yaml

from src import verticals
from tests.conftest import FIXTURE_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_raw() -> dict:
    return yaml.safe_load(FIXTURE_PATH.read_text())


def _write_and_load(tmp_path: Path, data: dict) -> verticals.VerticalsConfig:
    p = tmp_path / "verticals.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return verticals.load_verticals(p)


class TestFixtureHappyPath:
    def test_names_in_config_order(self, cfg):
        assert cfg.names == ("sap", "ai_eng", "risk_ai")
        assert cfg.default_vertical == "sap"

    def test_valid_verticals_includes_empty(self, cfg):
        assert cfg.valid_verticals == frozenset({"sap", "ai_eng", "risk_ai", ""})

    def test_rule_order_and_ownership(self, cfg):
        """Precedence, not rule count: strong SAP wins first, risk_ai outranks
        ai_eng, and the SAP-adjacent catch-all stays last."""
        owners = [v for v, _ in cfg.classifier_rules]
        assert owners[0] == "sap"
        assert owners[-1] == "sap"
        assert set(owners[1:-1]) <= {"risk_ai", "ai_eng"}
        assert owners.index("risk_ai") < owners.index("ai_eng")

    def test_patterns_match_sentinel_titles(self, cfg):
        """Walk the rules the way the classifier does, so adding rules to a
        vertical can't break this the way indexed unpacking did."""
        from src.cleaning import classify_vertical_from_title as classify

        assert classify("SAP ACM Functional Consultant") == "sap"
        assert classify("Model Risk Analyst") == "risk_ai"
        assert classify("AI Engineer") == "ai_eng"
        assert classify("Forward Deployed Engineer") == "ai_eng"
        assert classify("Machine Learning Engineer") == ""
        assert classify("Machine Learning Engineer, LLM Platform") == "ai_eng"
        assert classify("Risk and Controls Analyst") == "sap"
        assert classify("Senior Platform Engineer") == ""

    def test_term_shape(self, cfg):
        """Guards truncation/duplication without pinning counts that move
        every time a search term is tuned."""
        for name in cfg.names:
            v = cfg.verticals[name]
            assert v.search_terms, f"{name} has no search_terms"
            assert v.linkedin_terms, f"{name} has no linkedin_terms"
            assert len(set(v.search_terms)) == len(v.search_terms)
            assert len(set(v.linkedin_terms)) == len(v.linkedin_terms)
            assert set(v.linkedin_terms) <= set(v.search_terms)

    def test_skill_weights_blocks(self, cfg):
        assert set(cfg.verticals["sap"].skill_weights) == {"sap", "domain"}
        assert set(cfg.verticals["risk_ai"].skill_weights) == {"model_risk", "trading_quant"}
        assert cfg.verticals["sap"].skill_weights["sap"]["acm"] == 10


class TestByteParityAnchors:
    """Pin config strings against the pre-refactor src/scoring_io.py
    literals. If any of these fail, scored.parquet rows would drift."""

    def test_scored_by_stamps(self, cfg):
        assert cfg.verticals["sap"].disqualifier_scored_by == "rubric:sap-jd-years-disqualifier"
        assert cfg.verticals["risk_ai"].disqualifier_scored_by == "rubric:risk-ai-jd-disqualifier"

    def test_risk_ai_phrases(self, cfg):
        """Phrase lists are additive over time, so anchor the originals by
        membership; only the stamped reasoning text needs byte parity."""
        assert set(cfg.verticals["risk_ai"].disqualifier_phrases) >= {
            "phd required",
            "ph.d. required",
            "doctorate required",
            "cfa required",
            "cfa charter required",
            "frm required",
            "frm certification required",
            "loan portfolio",
            "actuary",
            "sanctions screening",
            "murex",
            "calypso",
        }
        assert cfg.verticals["sap"].disqualifier_phrases == (
            "successfactors",
            "payroll",
            "hcm",
            "sap pm",
            "sap pp",
            "production planning",
            "plant maintenance",
            "sap basis",
            "sap btp",
            "concur",
            "solution architect",
            "workday",
            "netsuite",
            "peoplesoft",
        )
        assert cfg.verticals["sap"].reasoning_phrase.startswith(
            "Auto-skipped: JD contains a sap disqualifier phrase"
        )

    def test_max_years(self, cfg):
        assert cfg.verticals["sap"].disqualifier_max_years == 4
        assert cfg.verticals["risk_ai"].disqualifier_max_years == 4

    def test_out_of_lane_reasoning_verbatim(self, cfg):
        assert cfg.out_of_lane_reasoning == (
            "Out-of-lane: title contains no in-lane keyword, so title_match=0 "
            "and the row cannot reach the fit>=50 shortlist."
        )

    def test_risk_ai_phrase_reasoning_verbatim(self, cfg):
        assert cfg.verticals["risk_ai"].reasoning_phrase == (
            "Auto-skipped: JD contains a risk_ai disqualifier phrase (PhD/CFA/FRM "
            "required, credit/actuarial/AML, front-office trading platform, or "
            "Salesforce/vendor-risk stack)."
        )

    def test_risk_ai_years_reasoning_verbatim(self, cfg):
        assert cfg.verticals["risk_ai"].reasoning_years == (
            "Auto-skipped: JD requires more years of experience than this "
            "vertical's max_years."
        )

    def test_sap_years_reasoning_verbatim(self, cfg):
        assert cfg.verticals["sap"].reasoning_years == (
            "Auto-skipped: JD requires more years of experience than this "
            "vertical's max_years."
        )


class TestValidation:
    def test_missing_file_names_the_template(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"verticals\.example\.yaml"):
            verticals.load_verticals(tmp_path / "nope.yaml")

    def test_bad_schema_version(self, tmp_path):
        data = _load_raw()
        data["schema_version"] = 2
        with pytest.raises(ValueError, match="schema_version"):
            _write_and_load(tmp_path, data)

    def test_bad_default_vertical(self, tmp_path):
        data = _load_raw()
        data["default_vertical"] = "nonexistent"
        with pytest.raises(ValueError, match="default_vertical"):
            _write_and_load(tmp_path, data)

    def test_unknown_rule_vertical(self, tmp_path):
        data = _load_raw()
        data["classifier_rules"][0]["vertical"] = "nonexistent"
        with pytest.raises(ValueError, match="classifier_rules"):
            _write_and_load(tmp_path, data)

    def test_noncompiling_pattern(self, tmp_path):
        data = _load_raw()
        data["classifier_rules"][0]["pattern"] = "(?:unclosed"
        with pytest.raises(ValueError, match="does not compile"):
            _write_and_load(tmp_path, data)

    def test_bad_vertical_name(self, tmp_path):
        data = _load_raw()
        data["verticals"]["Bad-Name"] = data["verticals"]["sap"]
        with pytest.raises(ValueError, match="must match"):
            _write_and_load(tmp_path, data)

    def test_missing_linkedin_terms(self, tmp_path):
        data = _load_raw()
        del data["verticals"]["sap"]["linkedin_terms"]
        with pytest.raises(ValueError, match="linkedin_terms"):
            _write_and_load(tmp_path, data)

    def test_phrases_without_reasoning_phrase(self, tmp_path):
        data = _load_raw()
        del data["verticals"]["risk_ai"]["disqualifier"]["reasoning_phrase"]
        with pytest.raises(ValueError, match="reasoning_phrase"):
            _write_and_load(tmp_path, data)

    def test_title_phrases_without_reasoning_title(self, tmp_path):
        data = _load_raw()
        del data["verticals"]["sap"]["disqualifier"]["reasoning_title"]
        with pytest.raises(ValueError, match="reasoning_title"):
            _write_and_load(tmp_path, data)

    def test_title_phrases_optional(self, tmp_path):
        data = _load_raw()
        del data["verticals"]["sap"]["disqualifier"]["title_phrases"]
        del data["verticals"]["sap"]["disqualifier"]["reasoning_title"]
        cfg = _write_and_load(tmp_path, data)
        assert cfg.verticals["sap"].disqualifier_title_phrases == ()
        assert cfg.verticals["sap"].reasoning_title is None

    def test_title_phrases_parsed(self, cfg):
        assert set(cfg.verticals["sap"].disqualifier_title_phrases) >= {
            "senior",
            "manager",
            "director",
            "principal",
            "lead",
            "architect",
            "head of",
            "vice president",
            "buyer",
            "sourcing",
            "procurement",
            "engineer",
        }
        assert cfg.verticals["sap"].reasoning_title.startswith(
            "Auto-skipped: title matches a sap title-disqualifier phrase"
        )
        assert set(cfg.verticals["risk_ai"].disqualifier_title_phrases) >= {
            "quality",
            "clinical",
            "privacy",
            "automation",
            "operations",
            "nurse",
        }
        assert cfg.verticals["risk_ai"].reasoning_title.startswith(
            "Auto-skipped: title matches a risk_ai title-disqualifier phrase"
        )


class TestExampleTemplate:
    """The committed template must never rot."""

    def test_example_yaml_loads(self):
        cfg = verticals.load_verticals(REPO_ROOT / "profile" / "verticals.example.yaml")
        assert cfg.names == ("example_primary", "example_secondary")
        assert cfg.default_vertical == "example_primary"

    @pytest.mark.parametrize("name", ["example_primary", "example_secondary"])
    @pytest.mark.parametrize("fname", ["rubric.md", "tailoring.md"])
    def test_example_dirs_have_prose_files(self, name, fname):
        assert (REPO_ROOT / "profile" / "verticals" / name / fname).is_file()


class TestSingleton:
    def test_set_config_wins(self, cfg):
        assert verticals.get_config() is cfg
