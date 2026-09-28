"""src/verticals.py loader tests: fixture happy path, verbatim carry-through
of the stamps and reasoning texts that land in scored.parquet, validation
failures, and the committed example template. Runs against the synthetic
tests/fixtures/verticals.yaml; the real config is covered by
tests/test_real_config_drift.py."""

from pathlib import Path

import pytest
import yaml

from src import verticals
from tests.conftest import FIXTURE_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_raw() -> dict:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


def _write_and_load(tmp_path: Path, data: dict) -> verticals.VerticalsConfig:
    p = tmp_path / "verticals.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return verticals.load_verticals(p)


class TestFixtureHappyPath:
    def test_names_in_config_order(self, cfg):
        assert cfg.names == ("example_primary", "example_secondary", "example_tertiary")
        assert cfg.default_vertical == "example_tertiary"

    def test_valid_verticals_includes_empty(self, cfg):
        assert cfg.valid_verticals == frozenset(
            {"example_primary", "example_secondary", "example_tertiary", ""}
        )

    def test_rule_order_and_ownership(self, cfg):
        """Precedence, not rule count: the strong primary signal wins first,
        example_secondary outranks example_tertiary, and the primary-adjacent
        catch-all stays last."""
        owners = [v for v, _ in cfg.classifier_rules]
        assert owners[0] == "example_primary"
        assert owners[-1] == "example_primary"
        assert set(owners[1:-1]) <= {"example_secondary", "example_tertiary"}
        assert owners.index("example_secondary") < owners.index("example_tertiary")

    def test_every_vertical_owns_at_least_one_rule(self, cfg):
        """A vertical with no classifier rule can never be reached by the title
        fallback, so an inbox clip for it silently lands out-of-lane. What each
        rule matches is pinned by the parametrized table in
        tests/discovery/test_normalize.py, not duplicated here."""
        owners = {vertical for vertical, _rule in cfg.classifier_rules}
        assert owners == set(cfg.names)

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

    def test_fixture_prose_and_resumes_exist_on_disk(self, cfg):
        """The fixture points at committed profile/verticals/example_*/ files;
        TestMainCli synthesizes its own, so nothing else catches a rename."""
        for name, v in cfg.verticals.items():
            for fname in ("rubric.md", "tailoring.md"):
                assert (REPO_ROOT / "profile" / "verticals" / name / fname).is_file()
            assert (REPO_ROOT / v.resume_file).is_file(), v.resume_file


class TestLaneRules:
    """/score's per-lane rules reach the dataclass as configured."""

    def test_drop_phrases(self, cfg):
        assert cfg.verticals["example_primary"].drop_phrases == (
            "rival widget suite",
            "legacy gizmo stack",
            "widget payroll module",
            "solution architect",
        )
        assert "phd required" in cfg.verticals["example_secondary"].drop_phrases

    def test_years_table_sorted_by_years(self, cfg):
        assert cfg.verticals["example_primary"].years == {3: 14, 4: 10}

    def test_skill_weights_kept_as_is(self, cfg):
        assert cfg.verticals["example_primary"].skill_weights["widgets"]["widget_assembly"] == 10


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
        data["verticals"]["Bad-Name"] = data["verticals"]["example_primary"]
        with pytest.raises(ValueError, match="must match"):
            _write_and_load(tmp_path, data)

    def test_missing_linkedin_terms(self, tmp_path):
        data = _load_raw()
        del data["verticals"]["example_primary"]["linkedin_terms"]
        with pytest.raises(ValueError, match="linkedin_terms"):
            _write_and_load(tmp_path, data)

    @pytest.mark.parametrize("key", ["title_exclude_terms",
                                     "title_include_terms",
                                     "title_strong_keep_terms"])
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_title_term_is_rejected(self, tmp_path, key, blank):
        """A blank term compiles to a pattern matching every title. In an
        exclude list that silently empties the whole vertical."""
        data = _load_raw()
        data["verticals"]["example_primary"].setdefault(key, []).append(blank)
        with pytest.raises(ValueError, match=key):
            _write_and_load(tmp_path, data)

    def test_old_disqualifier_block_names_its_replacement(self, tmp_path):
        data = _load_raw()
        data["verticals"]["example_primary"]["disqualifier"] = {"max_years": 4}
        with pytest.raises(ValueError, match="drop_phrases"):
            _write_and_load(tmp_path, data)

    def test_out_of_lane_block_rejected(self, tmp_path):
        data = _load_raw()
        data["out_of_lane"] = {"reasoning": "x"}
        with pytest.raises(ValueError, match="out_of_lane"):
            _write_and_load(tmp_path, data)

    @pytest.mark.parametrize("years", [{"3": 14}, {3: 25}, {3: -1}, [3, 4]])
    def test_bad_years_table(self, tmp_path, years):
        data = _load_raw()
        data["verticals"]["example_primary"]["years"] = years
        with pytest.raises(ValueError, match="years"):
            _write_and_load(tmp_path, data)


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

    def test_example_resume_files_exist(self):
        """resume_file is required and existence-checked, so a copied template
        must pass verticals-check without the user creating anything."""
        cfg = verticals.load_verticals(REPO_ROOT / "profile" / "verticals.example.yaml")
        for v in cfg.verticals.values():
            assert (REPO_ROOT / v.resume_file).is_file(), v.resume_file


class TestSingleton:
    def test_set_config_wins(self, cfg):
        assert verticals.get_config() is cfg


class TestFixtureMirrors:
    """The two fixtures must mirror each other. Drift here is invisible: the
    discovery suite keeps passing against rules the rest of the suite doesn't
    use."""

    FIXTURES = (
        REPO_ROOT / "tests" / "fixtures" / "verticals.yaml",
        REPO_ROOT / "tests" / "discovery" / "fixtures" / "verticals.yaml",
    )

    def test_mirrors_are_identical(self):
        # Bytes, not text: read_text translates newlines, so a CRLF mirror
        # decodes identically while no longer being byte-identical.
        a, b = (p.read_bytes() for p in self.FIXTURES)
        assert a == b, "tests/fixtures and tests/discovery/fixtures have diverged"

    def test_fixture_is_synthetic(self):
        """The fixtures ship publicly, so they must never be a copy of the
        gitignored real config."""
        real = REPO_ROOT / "profile" / "verticals.yaml"
        if not real.is_file():
            pytest.skip("profile/verticals.yaml is gitignored user data")
        expected = yaml.safe_load(real.read_text(encoding="utf-8"))
        for p in self.FIXTURES:
            assert yaml.safe_load(p.read_text(encoding="utf-8")) != expected, (
                f"{p.relative_to(REPO_ROOT)} is a copy of profile/verticals.yaml"
            )


class TestMainCli:
    """`uv run verticals-check` — every slash command's one-line prerequisite
    check, and the only thing standing between a half-onboarded vertical and a
    /score run that judges against the wrong resume."""

    def _setup(self, tmp_path, monkeypatch, *, drop_prose=(), drop_resume=()):
        """Build a self-contained repo root from the fixture config, minus any
        prose/resume files named in drop_*."""
        monkeypatch.setattr(verticals, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(verticals, "DEFAULT_CONFIG_PATH",
                            tmp_path / "profile" / "verticals.yaml")
        monkeypatch.setattr(verticals, "VERTICALS_DIR",
                            tmp_path / "profile" / "verticals")
        (tmp_path / "profile").mkdir()
        (tmp_path / "profile" / "verticals.yaml").write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        cfg = verticals.load_verticals(FIXTURE_PATH)
        for name, v in cfg.verticals.items():
            d = tmp_path / "profile" / "verticals" / name
            d.mkdir(parents=True)
            for fname in ("rubric.md", "tailoring.md"):
                if (name, fname) not in drop_prose:
                    (d / fname).write_text("# prose", encoding="utf-8")
            if name not in drop_resume:
                r = tmp_path / v.resume_file
                r.parent.mkdir(parents=True, exist_ok=True)
                r.write_text("# resume", encoding="utf-8")
        return cfg

    def test_success_prints_names_and_default(self, tmp_path, monkeypatch, capsys):
        cfg = self._setup(tmp_path, monkeypatch)
        assert verticals.main() == 0
        out = capsys.readouterr().out
        assert out.strip() == (
            f"verticals={','.join(cfg.names)} default={cfg.default_vertical}"
        )

    def test_missing_config_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(verticals, "DEFAULT_CONFIG_PATH",
                            tmp_path / "profile" / "verticals.yaml")
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert out.startswith("ERROR: ")
        assert "not found" in out
        # the pointer at the committed template is the actionable part
        assert "verticals.example.yaml" in out

    def test_malformed_config_reports_instead_of_raising(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        data = yaml.safe_load((tmp_path / "profile" / "verticals.yaml").read_text(encoding="utf-8"))
        data["schema_version"] = 2
        (tmp_path / "profile" / "verticals.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        assert verticals.main() == 1
        assert "schema_version must be 1" in capsys.readouterr().out

    def test_missing_rubric_is_named(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, drop_prose={("example_primary", "rubric.md")})
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert "missing per-vertical prose files" in out
        assert "example_primary/rubric.md" in out
        assert "example_primary/tailoring.md" not in out

    def test_missing_tailoring_is_named(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, drop_prose={("example_tertiary", "tailoring.md")})
        assert verticals.main() == 1
        assert "example_tertiary/tailoring.md" in capsys.readouterr().out

    def test_every_missing_prose_file_is_listed_not_just_the_first(
            self, tmp_path, monkeypatch, capsys):
        """A half-onboarded vertical usually misses several files; listing one
        per run turns onboarding into a guessing game."""
        self._setup(tmp_path, monkeypatch, drop_prose={
            ("example_primary", "rubric.md"), ("example_primary", "tailoring.md"),
            ("example_secondary", "rubric.md"),
        })
        assert verticals.main() == 1
        out = capsys.readouterr().out
        for expected in ("example_primary/rubric.md", "example_primary/tailoring.md", "example_secondary/rubric.md"):
            assert expected in out

    def test_missing_resume_file_is_named(self, tmp_path, monkeypatch, capsys):
        cfg = self._setup(tmp_path, monkeypatch, drop_resume={"example_primary"})
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert "missing per-vertical scoring resume files (resume_file)" in out
        assert cfg.verticals["example_primary"].resume_file in out

    def test_every_missing_resume_is_listed(self, tmp_path, monkeypatch, capsys):
        cfg = self._setup(tmp_path, monkeypatch, drop_resume={"example_primary", "example_secondary"})
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert cfg.verticals["example_primary"].resume_file in out
        assert cfg.verticals["example_secondary"].resume_file in out

    def test_prose_check_reports_before_the_resume_check(
            self, tmp_path, monkeypatch, capsys):
        """Both are broken; the prose error returns first. Pin the order so a
        reshuffle doesn't silently change what the user sees."""
        self._setup(tmp_path, monkeypatch,
                    drop_prose={("example_primary", "rubric.md")}, drop_resume={"example_primary"})
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert "prose files" in out
        assert "scoring resume files" not in out

    def test_a_dir_that_is_not_a_file_counts_as_missing(
            self, tmp_path, monkeypatch, capsys):
        """is_file(), not exists() — a directory named rubric.md is not prose."""
        self._setup(tmp_path, monkeypatch, drop_prose={("example_primary", "rubric.md")})
        (tmp_path / "profile" / "verticals" / "example_primary" / "rubric.md").mkdir()
        assert verticals.main() == 1
        assert "example_primary/rubric.md" in capsys.readouterr().out


class TestClassifierRuleProtocol:
    """Both rule kinds answer matches(title) -> bool. CompoundRule previously
    returned True/None purely to duck-type against re.Pattern.search, and had no
    coverage at all while being reachable from the live config."""

    def test_regex_rule_matches_are_case_insensitive(self, cfg):
        rule = next(r for v, r in cfg.classifier_rules
                    if isinstance(r, verticals.RegexRule))
        title = "Widget Assembly Functional Consultant"
        assert rule.matches(title) is True
        assert rule.matches(title.upper()) is True
        assert rule.matches(title.lower()) is True

    def test_compound_needs_the_match_and_one_requirement(self):
        rule = verticals.CompoundRule("Cog Learning", ("Platform", "Agents"))
        assert rule.matches("Cog Learning Engineer, Cog Platform") is True
        assert rule.matches("Cog Learning Engineer, Agents") is True
        # match string present, no requirement met
        assert rule.matches("Cog Learning Engineer") is False
        # requirement met, match string absent
        assert rule.matches("Platform Engineer") is False

    def test_compound_normalizes_hyphens_case_and_nbsp(self):
        """Boards write the same title three ways; a rule must not care."""
        rule = verticals.CompoundRule("Co-Pilot", ("Engineer",))
        for title in ("Co-Pilot Engineer", "co pilot engineer",
                      "CO PILOT ENGINEER", "Co-pilot Engineer"):
            assert rule.matches(title) is True, title

    def test_both_kinds_return_a_real_bool(self, cfg):
        """The classifier does `if rule.matches(title)`, and a rule that
        returned None would still work — until something compared the result."""
        for _vertical, rule in cfg.classifier_rules:
            assert isinstance(rule.matches("Some Unrelated Title"), bool), rule


class TestAbroadTerms:
    """Optional: absent means no abroad queries for that lane."""

    def test_absent_is_empty(self, cfg):
        assert all(v.abroad_terms == () for v in cfg.verticals.values())

    def test_a_list_is_loaded_in_order(self, tmp_path):
        data = _load_raw()
        data["verticals"]["example_primary"]["abroad_terms"] = ["Widget Engineer", "Junior Widget Engineer"]
        loaded = _write_and_load(tmp_path, data)
        assert loaded.verticals["example_primary"].abroad_terms == ("Widget Engineer", "Junior Widget Engineer")

    @pytest.mark.parametrize("bad", ["Widget Engineer", [""], [3]])
    def test_a_bad_value_is_rejected(self, tmp_path, bad):
        data = _load_raw()
        data["verticals"]["example_primary"]["abroad_terms"] = bad
        with pytest.raises(ValueError, match="abroad_terms must be a list"):
            _write_and_load(tmp_path, data)
