"""onboard-scaffold: the copies, the two YAML edits, and the safety contract.

Every test runs against a throwaway copy of the shipped templates with
`onboard_scaffold.PROFILE` repointed at it. Nothing here may touch the real
profile/ — it is a live install, and this CLI is a pile of cp example -> real.
"""
from __future__ import annotations

import builtins
import subprocess
from pathlib import Path

import pytest
import yaml

from src import onboard_scaffold as scaffold
from src.verticals import load_verticals

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_PROFILE = REPO_ROOT / "profile"

# What a default run must produce, derived from the shipped templates rather
# than listed, so a new template is covered the day it lands.
GATED = set(scaffold.OPTIONAL_TEMPLATES)

# A lane block with every key the strict loader requires, plus the one
# classifier rule /new-vertical's quick mode writes.
MINIMAL_LANE = """\
verticals:
  {name}:
    display_name: My Lane
    resume_file: profile/verticals/{name}/resume.md
    search_terms: [Some Title]
    linkedin_terms: [Some Title]
    disqualifier:
      max_years: 4
      scored_by: "rubric:{name}-jd-years-disqualifier"
      reasoning_years: "Auto-skipped: JD states an explicit minimum above the cap."
classifier_rules:
  - vertical: {name}
    pattern: '.'
"""


@pytest.fixture
def prof(tmp_path, monkeypatch) -> Path:
    """A scratch profile/ holding the real templates and the committed
    sponsorship default."""
    target = tmp_path / "profile"
    target.mkdir()
    for src in list(REAL_PROFILE.glob("*.example.*")) + [REAL_PROFILE / "sponsorship_rules.yaml"]:
        (target / src.name).write_bytes(src.read_bytes())
    monkeypatch.setattr(scaffold, "PROFILE", target)
    return target


def run(*argv: str) -> int:
    return scaffold.main(list(argv))


def _expected_default_targets(prof: Path) -> set[str]:
    return {
        scaffold._target_for(p).name
        for p in prof.glob("*.example.*")
        if p.name not in GATED
    }


class TestCopies:
    def test_a_fresh_scaffold_writes_every_default_template(self, prof, capsys):
        assert run("--vertical", "my_lane", "--work-auth", "citizen") == 0
        for name in _expected_default_targets(prof):
            assert (prof / name).is_file(), f"{name} not copied"
        assert (prof / "resume_template.docx").read_bytes() == (
            prof / "resume_template.example.docx").read_bytes()

    def test_the_answers_file_lands_without_the_apply_flag(self, prof):
        """/onboarding fills it from the resume it has already parsed, so the
        copy is on the default path; --with-apply only installs the browser."""
        assert run("--vertical", "my_lane", "--work-auth", "citizen") == 0
        assert (prof / "application_answers.yaml").is_file()

    def test_the_later_menu_templates_stay_out_of_the_default_path(self, prof):
        run("--vertical", "my_lane", "--work-auth", "citizen")
        for template in GATED:
            assert not (prof / scaffold._target_for(prof / template).name).exists()

    def test_with_optional_copies_the_later_menu_templates(self, prof):
        run("--vertical", "my_lane", "--work-auth", "citizen", "--with-optional")
        assert (prof / "voice_samples.md").is_file()
        assert (prof / "contacts.yaml").is_file()
        assert (prof / "companies.yaml").is_file()
        assert (prof / "pii_denylist.txt").is_file()

    def test_a_new_template_is_picked_up_without_a_code_change(self, prof):
        (prof / "brandnew.example.md").write_text("hello\n", encoding="utf-8")
        run("--vertical", "my_lane", "--work-auth", "citizen")
        assert (prof / "brandnew.md").read_text(encoding="utf-8") == "hello\n"


class TestSafetyContract:
    def test_rerunning_skips_everything_and_still_exits_zero(self, prof, capsys):
        assert run("--vertical", "my_lane", "--work-auth", "citizen") == 0
        capsys.readouterr()
        assert run("--vertical", "my_lane", "--work-auth", "citizen") == 0
        out = capsys.readouterr().out
        assert "SKIPPED" in out
        for name in _expected_default_targets(prof):
            assert f"{name} exists" in out

    def test_an_existing_file_is_never_clobbered(self, prof):
        (prof / "bullets.md").write_text("MINE\n", encoding="utf-8")
        run("--vertical", "my_lane", "--work-auth", "citizen")
        assert (prof / "bullets.md").read_text(encoding="utf-8") == "MINE\n"

    def test_force_overwrites(self, prof):
        (prof / "bullets.md").write_text("MINE\n", encoding="utf-8")
        run("--vertical", "my_lane", "--work-auth", "citizen", "--force")
        assert (prof / "bullets.md").read_text(encoding="utf-8") != "MINE\n"

    def test_dry_run_writes_nothing(self, prof, capsys):
        before = {p.name: p.read_bytes() for p in prof.iterdir()}
        assert run("--vertical", "my_lane", "--work-auth", "needs_now", "--dry-run") == 0
        after = {p.name: p.read_bytes() for p in prof.iterdir()}
        assert after == before
        out = capsys.readouterr().out
        assert "nothing written" in out
        assert "would copy" in out and "would strip" in out
        assert "would reconcile for needs_now" in out

    def test_dry_run_with_apply_only_prints_the_subprocesses(self, prof, capsys, monkeypatch):
        def _boom(*a, **k):  # noqa: ANN002
            raise AssertionError("--dry-run must not spawn anything")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert run("--vertical", "my_lane", "--work-auth", "citizen",
                   "--with-apply", "--dry-run") == 0
        out = capsys.readouterr().out
        assert "would run: uv sync --group apply" in out
        assert "would run: uv run playwright install chrome" in out

    def test_a_live_verticals_yaml_is_left_alone(self, prof, capsys):
        live = "schema_version: 1\n# my real config\n"
        (prof / "verticals.yaml").write_text(live, encoding="utf-8")
        run("--vertical", "my_lane", "--work-auth", "citizen")
        assert (prof / "verticals.yaml").read_text(encoding="utf-8") == live
        assert "strip not applied" in capsys.readouterr().out


class TestVerticalsStrip:
    @staticmethod
    def _stripped(prof: Path) -> Path:
        run("--vertical", "my_lane", "--work-auth", "citizen")
        return prof / "verticals.yaml"

    def test_no_example_lane_survives_anywhere_in_the_file(self, prof):
        text = self._stripped(prof).read_text(encoding="utf-8")
        for lane in scaffold.example_lane_names(prof / "verticals.example.yaml"):
            assert lane not in text

    def test_the_kept_keys_are_kept(self, prof):
        data = yaml.safe_load(self._stripped(prof).read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert data["out_of_lane"]["reasoning"]
        assert data["default_vertical"] == "my_lane"

    def test_the_only_thing_missing_is_the_lane_itself(self, prof):
        """The scaffolded file is one `/new-vertical` away from valid: the lane
        default_vertical names does not exist yet, and neither does its
        classifier rule. Nothing else about the file is wrong."""
        path = self._stripped(prof)
        with pytest.raises(ValueError) as excinfo:
            load_verticals(path)
        assert "verticals must be a nonempty mapping" in str(excinfo.value)

        text = path.read_text(encoding="utf-8")
        assert "verticals:" in text and "classifier_rules:" in text
        patched = text.replace("verticals:", "", 1).replace("classifier_rules:", "", 1)
        path.write_text(patched + MINIMAL_LANE.format(name="my_lane"), encoding="utf-8")

        cfg = load_verticals(path)
        assert cfg.default_vertical == "my_lane"
        assert cfg.names == ("my_lane",)
        assert len(cfg.classifier_rules) == 1

    def test_the_lane_set_is_derived_not_hardcoded(self):
        """R7: a vertical name in src/ is a boundary violation, and this strip
        is the one place tempted to hardcode one."""
        source = Path(scaffold.__file__).read_text(encoding="utf-8")
        for lane in scaffold.example_lane_names(REAL_PROFILE / "verticals.example.yaml"):
            assert lane not in source

    def test_a_renamed_example_lane_is_still_stripped(self, prof):
        """The derivation, not the outcome: rename a lane in the template and
        the strip must follow it."""
        example = prof / "verticals.example.yaml"
        text = example.read_text(encoding="utf-8").replace("example_primary", "zzz_renamed")
        example.write_text(text, encoding="utf-8")
        run("--vertical", "my_lane", "--work-auth", "citizen")
        assert "zzz_renamed" not in (prof / "verticals.yaml").read_text(encoding="utf-8")


class TestWorkAuth:
    @staticmethod
    def _rules(prof: Path) -> dict:
        return yaml.safe_load((prof / "sponsorship_rules.yaml").read_text(encoding="utf-8"))

    @pytest.mark.parametrize("status", ["citizen", "time_limited"])
    def test_authorized_now_is_a_no_op(self, prof, status):
        before = (prof / "sponsorship_rules.yaml").read_text(encoding="utf-8")
        assert run("--vertical", "my_lane", "--work-auth", status) == 0
        assert (prof / "sponsorship_rules.yaml").read_text(encoding="utf-8") == before

    def test_needs_now_moves_opt_ok_into_ineligible_and_empties_the_guard(self, prof):
        before = self._rules(prof)
        assert before["opt_ok"] and before["false_positive_guard"]
        assert run("--vertical", "my_lane", "--work-auth", "needs_now") == 0
        after = self._rules(prof)
        assert after["opt_ok"] == []
        assert after["false_positive_guard"] == []
        assert after["ineligible"] == before["ineligible"] + before["opt_ok"]
        assert after["hard_ineligible"] == before["hard_ineligible"]
        assert after["sponsors"] == before["sponsors"]

    def test_needs_now_keeps_the_header_that_explains_the_edit(self, prof):
        run("--vertical", "my_lane", "--work-auth", "needs_now")
        text = (prof / "sponsorship_rules.yaml").read_text(encoding="utf-8")
        assert "THESE LISTS ASSUME YOU ARE ALREADY AUTHORIZED TO WORK" in text

    def test_needs_now_is_idempotent(self, prof, capsys):
        run("--vertical", "my_lane", "--work-auth", "needs_now")
        once = (prof / "sponsorship_rules.yaml").read_text(encoding="utf-8")
        capsys.readouterr()
        assert run("--vertical", "my_lane", "--work-auth", "needs_now") == 0
        assert (prof / "sponsorship_rules.yaml").read_text(encoding="utf-8") == once
        assert "already reconciled" in capsys.readouterr().out

    def test_no_duplicate_phrase_when_one_is_already_ineligible(self, prof):
        path = prof / "sponsorship_rules.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'ineligible:\n', 'ineligible:\n  - "no sponsorship"\n', 1),
            encoding="utf-8")
        run("--vertical", "my_lane", "--work-auth", "needs_now")
        ineligible = self._rules(prof)["ineligible"]
        assert ineligible.count("no sponsorship") == 1

    def test_the_three_choices_match_the_preferences_template(self):
        """The reconcile hangs on there being exactly one variant that is not
        authorized to work now."""
        prefs = (REAL_PROFILE / "preferences.example.md").read_text(encoding="utf-8").lower()
        assert "citizen / permanent resident" in prefs
        assert "needs sponsorship now" in prefs
        assert "time-limited work authorization" in prefs
        assert set(scaffold.WORK_AUTH_CHOICES) == {"citizen", "needs_now", "time_limited"}


class TestLibpostalProbe:
    def test_a_missing_libpostal_is_advisory_not_fatal(self, prof, monkeypatch, capsys):
        real_import = builtins.__import__

        def _no_postal(name, *args, **kwargs):
            if name.startswith("postal"):
                raise ImportError("no libpostal")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_postal)
        assert run("--vertical", "my_lane", "--work-auth", "citizen") == 0
        out = capsys.readouterr().out
        assert "libpostal not importable" in out
        assert "uv sync --group discovery" in out


class TestApplyPath:
    def _record(self, monkeypatch, returncodes):
        calls = []

        def _fake(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, returncodes[len(calls) - 1])

        monkeypatch.setattr(subprocess, "run", _fake)
        return calls

    def test_with_apply_runs_both_commands(self, prof, monkeypatch):
        calls = self._record(monkeypatch, [0, 0])
        assert run("--vertical", "my_lane", "--work-auth", "citizen", "--with-apply") == 0
        assert [c[0] for c in calls] == [
            ["uv", "sync", "--group", "apply"],
            ["uv", "run", "playwright", "install", "chrome"],
        ]
        assert calls[1][1]["env"]["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"

    def test_a_failed_subprocess_exits_nonzero_and_keeps_what_was_written(
        self, prof, monkeypatch, capsys
    ):
        self._record(monkeypatch, [1, 0])
        assert run("--vertical", "my_lane", "--work-auth", "citizen", "--with-apply") == 1
        assert (prof / "bullets.md").is_file()  # no rollback
        assert (prof / "verticals.yaml").is_file()
        assert "exited 1" in capsys.readouterr().err

    def test_no_subprocess_runs_without_the_flag(self, prof, monkeypatch):
        calls = self._record(monkeypatch, [])
        assert run("--vertical", "my_lane", "--work-auth", "citizen") == 0
        assert calls == []


class TestVerticalName:
    """An invalid lane name writes an unloadable default_vertical, and a re-run
    skips the existing file rather than repairing it."""

    @pytest.mark.parametrize("name", ["My Lane", "Revenue-Ops", "1lane", "", "lane!"])
    def test_an_invalid_name_exits_nonzero_and_writes_nothing(self, prof, name, capsys):
        assert run("--vertical", name, "--work-auth", "citizen") != 0
        assert not list(prof.glob("verticals.yaml"))
        assert "^[a-z][a-z0-9_]*$" in capsys.readouterr().err

    @pytest.mark.parametrize("name", ["lane", "revenue_ops", "lane2"])
    def test_a_valid_name_is_accepted(self, prof, name):
        assert run("--vertical", name, "--work-auth", "citizen") == 0

    def test_the_pattern_is_the_loader_pattern(self):
        from src import verticals as v
        assert scaffold.VERTICAL_NAME_RE.pattern == v._NAME_RE.pattern


def test_the_real_profile_is_never_a_target(prof):
    """The fixture repoints PROFILE; this asserts the module reads it through
    the module attribute rather than capturing paths.PROFILE at import."""
    run("--vertical", "my_lane", "--work-auth", "citizen")
    assert scaffold._templates(False)
    assert all(p.parent == prof for p in scaffold._templates(True))
