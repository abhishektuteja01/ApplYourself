"""src/applyourself/shared/verticals.py loader tests: fixture happy path, verbatim carry-through
of the stamps and reasoning texts that land in scored.parquet, validation
failures, and the committed example template. Runs against the synthetic
tests/fixtures/verticals.yaml; the real config is covered by
tests/repo/test_real_config_drift.py."""

from pathlib import Path

import pytest
import yaml

from applyourself.shared import paths, verticals
from tests.conftest import FIXTURE_PATH

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_raw() -> dict:
    """The fixture's top-level file, with each lane's lane.yaml under `verticals`."""
    data = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    data["verticals"] = {
        n: yaml.safe_load(verticals.lane_file(FIXTURE_PATH, n).read_text(encoding="utf-8"))
        for n in data["lanes"]
    }
    return data


def _dump(data) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _write(folder: Path, data: dict) -> Path:
    """data as _load_raw shapes it, written back as verticals.yaml plus lane files."""
    top = dict(data)
    lanes = top.pop("verticals")
    top["lanes"] = list(lanes)
    p = folder / "verticals.yaml"
    folder.mkdir(parents=True, exist_ok=True)
    p.write_text(_dump(top), encoding="utf-8")
    for name, lane in lanes.items():
        lf = verticals.lane_file(p, name)
        lf.parent.mkdir(parents=True, exist_ok=True)
        lf.write_text(_dump(lane), encoding="utf-8")
    return p


def _write_and_load(tmp_path: Path, data: dict) -> verticals.VerticalsConfig:
    return verticals.load_verticals(_write(tmp_path, data))


class TestFixtureHappyPath:
    def test_names_in_config_order(self, cfg):
        assert cfg.names == ("example_primary", "example_secondary", "example_tertiary")
        assert cfg.default_vertical == "example_tertiary"

    def test_rule_order_and_ownership(self, cfg):
        """Precedence, not rule count: the strong primary signal wins first,
        example_secondary outranks example_tertiary, and the primary-adjacent
        catch-all stays last among the pattern rules (`terms` rules follow)."""
        owners = [v for v, r in cfg.classifier_rules if not isinstance(r, verticals.TermsRule)]
        assert owners[0] == "example_primary"
        assert owners[-1] == "example_primary"
        assert set(owners[1:-1]) <= {"example_secondary", "example_tertiary"}
        assert owners.index("example_secondary") < owners.index("example_tertiary")

    def test_every_vertical_owns_at_least_one_rule(self, cfg):
        """A vertical with no classifier rule can never be reached by the title
        fallback, so an inbox clip for it silently lands out-of-lane. What each
        rule matches is pinned by the parametrized table in
        tests/shared/test_identity.py, not duplicated here."""
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

    def test_fixture_prose_exists_and_every_lane_has_a_resume_shape(self, cfg):
        """The fixture points at committed profile/verticals/example_*/ files;
        TestMainCli synthesizes its own, so nothing else catches a rename."""
        for name, v in cfg.verticals.items():
            for fname in ("rubric.md", "tailoring.md"):
                assert (REPO_ROOT / "profile" / "verticals" / name / fname).is_file()
            assert v.resume is not None, name


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

    def test_score_rules_merge_key_by_key_over_defaults(self, cfg):
        primary, secondary = cfg.verticals["example_primary"], cfg.verticals["example_secondary"]
        assert (primary.score.cutoff, secondary.score.cutoff) == (80, 78)
        assert secondary.score.level == primary.score.level  # the default, not the code's
        assert secondary.score.years_zero_at == 8


class TestValidation:
    def test_missing_file_names_the_template(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"verticals\.example\.yaml"):
            verticals.load_verticals(tmp_path / "nope.yaml")

    def test_bad_schema_version(self, tmp_path):
        data = _load_raw()
        data["schema_version"] = 3
        with pytest.raises(ValueError, match="schema_version"):
            _write_and_load(tmp_path, data)

    def test_a_schema_1_file_says_where_the_lanes_moved(self, tmp_path):
        p = tmp_path / "verticals.yaml"
        p.write_text(_dump({"schema_version": 1, "verticals": {}}), encoding="utf-8")
        with pytest.raises(
            ValueError, match=r"moved to verticals/<lane>/lane\.yaml.*config migrate"
        ):
            verticals.load_verticals(p)

    def test_a_listed_lane_with_no_file_is_named(self, tmp_path):
        p = _write(tmp_path, _load_raw())
        verticals.lane_file(p, "example_secondary").unlink()
        with pytest.raises(ValueError, match="example_secondary has no"):
            verticals.load_verticals(p)

    def test_defaults_lists_follow_the_lane_and_other_keys_are_replaced(self, tmp_path):
        data = _load_raw()
        data["defaults"] = {"display_name": "Shared", "drop_phrases": ["shared phrase"]}
        lane = data["verticals"]["example_primary"]
        cfg = _write_and_load(tmp_path, data)
        got = cfg.verticals["example_primary"]
        assert got.display_name == lane["display_name"]
        assert got.drop_phrases == (*lane["drop_phrases"], "shared phrase")
        assert got.score == verticals._parse_score({}, tmp_path, "score")  # the code defaults

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

    def test_a_typo_key_names_the_closest_valid_key(self, tmp_path):
        data = _load_raw()
        lane = data["verticals"]["example_primary"]
        lane["title_exclude_term"] = lane.pop("title_exclude_terms", ["x"])
        with pytest.raises(
            ValueError, match="'title_exclude_term' \\(did you mean 'title_exclude_terms'"
        ):
            _write_and_load(tmp_path, data)

    def test_missing_linkedin_terms_reads_search_terms_but_a_bad_one_fails(self, tmp_path):
        data = _load_raw()
        del data["verticals"]["example_primary"]["linkedin_terms"]
        lane = _write_and_load(tmp_path, data).verticals["example_primary"]
        assert lane.linkedin_terms == lane.search_terms
        data["verticals"]["example_primary"]["linkedin_terms"] = "Widget Engineer"
        with pytest.raises(ValueError, match="linkedin_terms must be a list"):
            _write_and_load(tmp_path, data)
        data["verticals"]["example_primary"]["linkedin_terms"] = []
        with pytest.raises(ValueError, match="linkedin_terms is empty: leave it out"):
            _write_and_load(tmp_path, data)

    @pytest.mark.parametrize(
        "key", ["title_exclude_terms", "title_include_terms", "title_strong_keep_terms"]
    )
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_title_term_is_rejected(self, tmp_path, key, blank):
        """A blank term compiles to a pattern matching every title. In an
        exclude list that silently empties the whole vertical."""
        data = _load_raw()
        data["verticals"]["example_primary"].setdefault(key, []).append(blank)
        with pytest.raises(ValueError, match=key):
            _write_and_load(tmp_path, data)

    @pytest.mark.parametrize(
        "score, where",
        [
            ({"level": {"entry": 0, "mid": 5}}, "score.level must set each"),
            ({"years": {"3": 5}}, "score.years must map"),
            ({"years": {3: 101}}, r"score.years.3 must be"),
            ({"years_zero_at": 0}, "score.years_zero_at must be"),
            ({"cutoff": "76"}, "score.cutoff must be"),
            ({"cutof": 76}, "cutof"),
        ],
    )
    def test_bad_score_block(self, tmp_path, score, where):
        data = _load_raw()
        data["verticals"]["example_primary"]["score"] = score
        with pytest.raises(ValueError, match=where):
            _write_and_load(tmp_path, data)

    def test_old_judge_keys_are_still_read(self, tmp_path):
        data = _load_raw()
        data["verticals"]["example_primary"]["focus"] = {"question": "Q?", "yes_min": 18}
        assert "example_primary" in _write_and_load(tmp_path, data).verticals


class TestExampleTemplate:
    """The committed template must never rot."""

    def test_example_yaml_loads(self):
        cfg = verticals.load_verticals(REPO_ROOT / "profile" / "verticals.example.yaml")
        assert cfg.names == ("example_primary", "example_secondary", "example_tertiary")
        assert cfg.default_vertical == "example_primary"

    @pytest.mark.parametrize("name", ["example_primary", "example_secondary", "example_tertiary"])
    @pytest.mark.parametrize("fname", ["rubric.md", "tailoring.md"])
    def test_example_dirs_have_prose_files(self, name, fname):
        assert (REPO_ROOT / "profile" / "verticals" / name / fname).is_file()


class TestSingleton:
    def test_set_config_wins(self, cfg):
        assert verticals.get_config() is cfg


class TestFixture:
    FIXTURE = REPO_ROOT / "tests" / "fixtures" / "verticals.yaml"

    def test_fixture_is_synthetic(self):
        """The fixtures ship publicly, so they must never be a copy of the
        gitignored real config."""
        real = REPO_ROOT / "profile" / "verticals.yaml"
        if not real.is_file():
            pytest.skip("profile/verticals.yaml is gitignored user data")
        expected = yaml.safe_load(real.read_text(encoding="utf-8"))
        assert yaml.safe_load(self.FIXTURE.read_text(encoding="utf-8")) != expected, (
            "tests/fixtures/verticals.yaml is a copy of profile/verticals.yaml"
        )


class TestMainCli:
    """`uv run verticals-check` — every slash command's one-line prerequisite
    check, and the only thing standing between a half-onboarded vertical and a
    /score run that judges against the wrong resume."""

    def _setup(self, tmp_path, monkeypatch, *, drop_prose=()):
        """Build a self-contained repo root from the fixture config, minus any
        prose files named in drop_prose."""
        monkeypatch.delitem(paths._overrides, "VERTICALS_DIR")
        _write(tmp_path / "profile", _load_raw())
        cfg = verticals.load_verticals(FIXTURE_PATH)
        for name in cfg.verticals:
            d = tmp_path / "profile" / "verticals" / name
            for fname in ("rubric.md", "tailoring.md"):
                if (name, fname) not in drop_prose:
                    (d / fname).write_text("# prose", encoding="utf-8")
        return cfg

    def test_success_prints_names_and_default(self, tmp_path, monkeypatch, capsys):
        cfg = self._setup(tmp_path, monkeypatch)
        assert verticals.main() == 0
        out = capsys.readouterr().out
        assert out.strip() == (f"verticals={','.join(cfg.names)} default={cfg.default_vertical}")

    def test_missing_config_file(self, tmp_path, monkeypatch, capsys):
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert out.startswith("ERROR: ")
        assert "not found" in out
        # the pointer at the committed template is the actionable part
        assert "verticals.example.yaml" in out

    def test_malformed_config_reports_instead_of_raising(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        data = yaml.safe_load((tmp_path / "profile" / "verticals.yaml").read_text(encoding="utf-8"))
        data["schema_version"] = 3
        (tmp_path / "profile" / "verticals.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        assert verticals.main() == 1
        assert "schema_version must be 2" in capsys.readouterr().out

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
        self, tmp_path, monkeypatch, capsys
    ):
        """A half-onboarded vertical usually misses several files; listing one
        per run turns onboarding into a guessing game."""
        self._setup(
            tmp_path,
            monkeypatch,
            drop_prose={
                ("example_primary", "rubric.md"),
                ("example_primary", "tailoring.md"),
                ("example_secondary", "rubric.md"),
            },
        )
        assert verticals.main() == 1
        out = capsys.readouterr().out
        for expected in (
            "example_primary/rubric.md",
            "example_primary/tailoring.md",
            "example_secondary/rubric.md",
        ):
            assert expected in out

    def test_a_lane_without_a_resume_block_is_named(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        lane_file = tmp_path / "profile" / "verticals" / "example_tertiary" / "lane.yaml"
        data = yaml.safe_load(lane_file.read_text(encoding="utf-8"))
        del data["resume"]
        lane_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert "no `resume:` block" in out and "example_tertiary" in out

    def test_a_source_problem_is_reported(self, tmp_path, monkeypatch, capsys):

        self._setup(tmp_path, monkeypatch)
        broken = tmp_path / "bullets.md"
        broken.write_text(
            (REPO_ROOT / "profile" / "bullets.example.md")
            .read_text(encoding="utf-8")
            .replace("## B-WID-01", "## B-ZZZ-01"),
            encoding="utf-8",
        )
        monkeypatch.setitem(paths._overrides, "BULLETS", broken)
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert "problem(s) in the resume sources" in out and "no context 'ZZZ'" in out

    def test_prose_check_reports_before_the_resume_check(self, tmp_path, monkeypatch, capsys):
        """Both are broken; the prose error returns first. Pin the order so a
        reshuffle doesn't silently change what the user sees."""
        self._setup(tmp_path, monkeypatch, drop_prose={("example_primary", "rubric.md")})
        assert verticals.main() == 1
        out = capsys.readouterr().out
        assert "prose files" in out
        assert "scoring resume files" not in out

    def test_a_dir_that_is_not_a_file_counts_as_missing(self, tmp_path, monkeypatch, capsys):
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
        rule = next(r for v, r in cfg.classifier_rules if isinstance(r, verticals.RegexRule))
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
        for title in (
            "Co-Pilot Engineer",
            "co pilot engineer",
            "CO PILOT ENGINEER",
            "Co-pilot Engineer",
        ):
            assert rule.matches(title) is True, title

    def test_compound_never_starts_inside_a_word(self):
        rule = verticals.CompoundRule("ML Engineer", ("Platform",))
        assert rule.matches("ML-Engineering Lead, Platform") is True
        assert rule.matches("HTML Engineer, Platform") is False

    def test_terms_rule_reads_endings_and_variants(self):
        rule = verticals.TermsRule(("forward deployed",), (("engineer", "ingenieur"),))
        assert rule.matches("Forward-Deployment Engineers") is True
        assert verticals.TermsRule(("cog engineer",), (("engineer", "ingenieur"),)).matches(
            "Cog-Ingenieur (m/w/d)"
        )

    def test_a_rule_takes_pattern_or_terms_not_both(self, tmp_path):
        data = _load_raw()
        data["classifier_rules"].append(
            {"vertical": "example_primary", "pattern": "x", "terms": ["y"]}
        )
        with pytest.raises(ValueError, match="exactly one of `pattern` or `terms`"):
            _write_and_load(tmp_path, data)

    def test_both_kinds_return_a_real_bool(self, cfg):
        """The classifier does `if rule.matches(title)`, and a rule that
        returned None would still work — until something compared the result."""
        for _vertical, rule in cfg.classifier_rules:
            assert isinstance(rule.matches("Some Unrelated Title"), bool), rule


class TestAbroadTerms:
    """Optional: absent, a lane listed in `abroad` searches its search_terms."""

    def test_absent_is_the_search_terms(self, cfg):
        assert all(v.abroad_terms == v.search_terms for v in cfg.verticals.values())

    def test_a_list_is_loaded_in_order(self, tmp_path):
        data = _load_raw()
        data["verticals"]["example_primary"]["abroad_terms"] = [
            "Widget Engineer",
            "Junior Widget Engineer",
        ]
        loaded = _write_and_load(tmp_path, data)
        assert loaded.verticals["example_primary"].abroad_terms == (
            "Widget Engineer",
            "Junior Widget Engineer",
        )

    @pytest.mark.parametrize("bad", ["Widget Engineer", [""], [3]])
    def test_a_bad_value_is_rejected(self, tmp_path, bad):
        data = _load_raw()
        data["verticals"]["example_primary"]["abroad_terms"] = bad
        with pytest.raises(ValueError, match="abroad_terms must be a list"):
            _write_and_load(tmp_path, data)
