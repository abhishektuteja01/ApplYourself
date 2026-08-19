"""Every profile/ input the pipeline reads must be shippable to a new user.

The failure this prevents is silent: a command references profile/<something>,
that file is gitignored user data, and nothing tells a new user it needs to
exist or what shape it takes. Before these tests, six of them had no template.

The coverage check derives its list from the commands and modules themselves
rather than a hardcoded inventory, so a newly referenced profile file fails here
until it is either committed or templated.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from src.verticals import load_verticals

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE = REPO_ROOT / "profile"

_PATH_RE = re.compile(r"profile/[A-Za-z0-9_./<>*-]+")
# A path with one of these is a pattern, not a file: profile/verticals/<name>/,
# profile/*, profile/synonyms_draft_YYYY-MM-DD.md.
_PLACEHOLDER_MARKERS = ("<", ">", "*", "YYYY", "MM", "DD")

# Committed defaults: real files, not templates, because they are rules rather
# than personal data. A new clone gets working copies and tunes them in place.
COMMITTED_DEFAULTS = {
    "profile/de_ai_rules.yaml",
    "profile/sponsorship_rules.yaml",
}

# Suffix -> template suffix.
_TEMPLATE_SUFFIXES = {
    ".md": ".example.md",
    ".yaml": ".example.yaml",
    ".yml": ".example.yml",
    ".txt": ".example.txt",
    ".docx": ".example.docx",
}


_BINARY_SUFFIXES = {".pyc", ".pyo", ".so", ".docx", ".png", ".jpg", ".pdf"}


def _text_files(root: Path) -> list[Path]:
    """Every readable text file under `root`, recursively and regardless of
    extension. `.githooks/pre-push` has no suffix and `scripts/` may nest, so an
    extension-keyed non-recursive glob missed real referrers."""
    if not root.exists():
        return []
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix.lower() not in _BINARY_SUFFIXES
    )


def _source_groups() -> dict[str, list[Path]]:
    """The three kinds of file that can name a profile input."""
    return {
        "commands": list((REPO_ROOT / ".claude").rglob("*.md")),
        "modules": list((REPO_ROOT / "src").rglob("*.py")),
        # Recursive, and extension-agnostic for the hook dir: .githooks/pre-push
        # has no suffix at all, and it names profile/pii_denylist.txt plus both
        # example .docx. A non-recursive *.sh/*.py glob saw none of it.
        "scripts": (
            _text_files(REPO_ROOT / "scripts")
            + list((REPO_ROOT / ".github").rglob("*.yml"))
            + _text_files(REPO_ROOT / ".githooks")
        ),
    }


def _literal_strings(node) -> list[str]:
    """Every string constant in a list/tuple, one level of nesting deep, so both
    ["a.md", "b.md"] and [("a.md", "hint"), ...] yield their filenames."""
    out = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                out.append(element.value)
            elif isinstance(element, (ast.List, ast.Tuple)) and element.elts:
                first = element.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    out.append(first.value)
    return out


def _profile_filenames_from_ast(path: Path) -> set[str]:
    """Bare filenames a module joins onto a PROFILE constant.

    The regex below cannot see these: every src/ module that reads a profile file
    builds the path from `paths.PROFILE / "name.md"` or from a module-level
    PROFILE_FILES list, so the literal string "profile/" never appears.
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _is_profile_ref(node) -> bool:
        if isinstance(node, ast.Attribute):
            return node.attr == "PROFILE"
        return isinstance(node, ast.Name) and node.id == "PROFILE"

    for node in ast.walk(tree):
        # paths.PROFILE / "bullets.md"
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and _is_profile_ref(node.left)
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
        ):
            names.add(node.right.value)
        # REQUIRED_PROFILE_FILES = [("bullets.md", "hint"), ...]
        # AnnAssign as well as Assign: the real one carries an annotation
        # (`REQUIRED_PROFILE_FILES: list[tuple[str, str]] = [...]`), and matching
        # only Assign silently found nothing in the file that matters most.
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any("PROFILE" in t for t in targets):
                names.update(_literal_strings(node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if "PROFILE" in node.target.id and node.value is not None:
                names.update(_literal_strings(node.value))
    return {n for n in names if "." in n and "/" not in n}


def _referenced_profile_paths() -> dict[str, set[str]]:
    """{profile-relative path: {files that reference it}} for concrete paths."""
    found: dict[str, set[str]] = {}
    for paths_in_group in _source_groups().values():
        for path in paths_in_group:
            rel = str(path.relative_to(REPO_ROOT))
            text = path.read_text(encoding="utf-8")
            for match in _PATH_RE.findall(text):
                cleaned = match.rstrip(".,);`'\"")
                if any(marker in cleaned for marker in _PLACEHOLDER_MARKERS):
                    continue
                if "." not in cleaned.rsplit("/", 1)[-1]:
                    continue  # a directory, not a file
                found.setdefault(cleaned, set()).add(rel)
            if path.suffix == ".py":
                for name in _profile_filenames_from_ast(path):
                    found.setdefault(f"profile/{name}", set()).add(rel)
    return found


def _template_for(rel_path: str) -> str | None:
    p = Path(rel_path)
    suffix = "".join(p.suffixes[-1:])
    replacement = _TEMPLATE_SUFFIXES.get(suffix)
    if replacement is None:
        return None
    return str(p.with_name(p.name[: -len(suffix)] + replacement))


class TestTemplateCoverage:
    def test_every_referenced_profile_file_is_shipped_or_templated(self):
        missing = []
        for rel_path, referrers in sorted(_referenced_profile_paths().items()):
            if ".example." in rel_path or rel_path in COMMITTED_DEFAULTS:
                continue
            if "/verticals/example_" in rel_path:
                continue  # committed example lane files
            if Path(rel_path).name.startswith("."):
                continue  # runtime state a command writes, not a user input
            template = _template_for(rel_path)
            if template is None:
                continue
            if not (REPO_ROOT / template).exists():
                missing.append(
                    f"{rel_path} (referenced by {', '.join(sorted(referrers))}) "
                    f"has no template at {template}"
                )
        assert not missing, "profile inputs with no template:\n  " + "\n  ".join(
            missing
        )

    def test_the_scan_actually_finds_things(self):
        """A scan that silently stops matching would make the test above pass
        vacuously forever."""
        found = _referenced_profile_paths()
        assert len(found) >= 10, f"reference scan found only {len(found)} paths"
        assert "profile/bullets.md" in found
        # A second anchor on a different pipeline surface: with only the
        # bullets.md anchor, the whole /apply config surface could drop out of
        # the scan and every test above would stay green.
        assert "profile/application_answers.yaml" in found

    def test_every_source_group_is_non_empty(self):
        """A renamed or moved directory would drop a whole group from the scan
        without failing anything above."""
        for group, paths_in_group in _source_groups().items():
            assert paths_in_group, f"source group {group!r} matched no files"

    def test_the_ast_pass_finds_the_idiom_the_regex_cannot(self):
        """Guards the AST pass specifically: every src/ module builds profile
        paths from a constant plus a bare filename, so the regex sees none of
        them. If this stops finding anything, coverage silently narrows to
        whatever the command prose happens to mention."""
        from_ast: set[str] = set()
        for path in (REPO_ROOT / "src").rglob("*.py"):
            from_ast |= _profile_filenames_from_ast(path)
        assert from_ast, "AST pass found no PROFILE-joined filenames"
        assert "de_ai_rules.yaml" in from_ast
        assert "bullets.md" in from_ast

    def test_an_unknown_extension_is_not_silently_exempt(self):
        """_template_for returns None for an unmapped suffix, and the coverage
        loop skips None. That must stay a deliberate, enumerated decision rather
        than a hole: anything referenced with an unmapped extension fails here."""
        unmapped = []
        for rel_path in _referenced_profile_paths():
            if ".example." in rel_path or rel_path in COMMITTED_DEFAULTS:
                continue
            if "/verticals/example_" in rel_path or Path(rel_path).name.startswith("."):
                continue
            if _template_for(rel_path) is None:
                unmapped.append(rel_path)
        assert not unmapped, (
            f"referenced profile files whose extension has no template mapping, "
            f"so the coverage test skips them entirely: {unmapped}. Add the "
            f"suffix to _TEMPLATE_SUFFIXES."
        )

    @pytest.mark.parametrize(
        "name",
        [
            "bullets.example.md",
            "skills_master.example.md",
            "preferences.example.md",
            "voice_samples.example.md",
            "scoring_rubric.example.md",
            "contacts.example.yaml",
            "companies.example.yaml",
            "discovery.example.yaml",
            "application_answers.example.yaml",
            "verticals.example.yaml",
            "pii_denylist.example.txt",
            "resume_template.example.docx",
            "cover_letter_template.example.docx",
        ],
    )
    def test_template_exists_and_is_not_empty(self, name):
        path = PROFILE / name
        assert path.exists(), f"{path} missing"
        assert path.stat().st_size > 0, f"{path} is empty"


class TestExampleConfigsAreValid:
    def test_verticals_example_loads_through_the_real_loader(self):
        """The strict loader is what a new user hits on their first command."""
        cfg = load_verticals(PROFILE / "verticals.example.yaml")
        assert cfg.default_vertical in cfg.names
        assert cfg.classifier_rules

    def test_application_answers_example_loads_through_the_real_loader(self, tmp_path):
        """`load_answers` is the strict validator /apply hits, and existence +
        non-empty said nothing about schema validity: a template with a bad
        work_authorization block or an unknown top-level key shipped green.

        The cross-check against preferences.md is fed a synthesized file rather
        than preferences.example.md, which deliberately states all three
        statuses so the new user has to choose one."""
        from src.apply.answers import load_answers, preferences_statuses

        answers_path = PROFILE / "application_answers.example.yaml"
        declared = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
        status = declared["work_authorization"]["status"]

        # Reuse the template's own wording for that status, so this stays a real
        # cross-check rather than a hand-written stand-in.
        lines = [
            line
            for line in (PROFILE / "preferences.example.md").read_text(
                encoding="utf-8"
            ).splitlines()
            if preferences_statuses(f"## Work authorization\n{line}\n") == {status}
        ]
        assert lines, f"preferences.example.md states no line for status {status!r}"
        prefs = tmp_path / "preferences.md"
        prefs.write_text("## Work authorization\n\n" + lines[0] + "\n", encoding="utf-8")

        answers = load_answers(answers_path, prefs)
        assert answers.status == status
        assert answers.identity
        assert answers.education

    @pytest.mark.parametrize(
        "name", ["contacts.example.yaml", "companies.example.yaml", "discovery.example.yaml"]
    )
    def test_yaml_templates_parse(self, name):
        data = yaml.safe_load((PROFILE / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert data.get("schema_version") == 1

    def test_contacts_template_entries_carry_the_documented_fields(self):
        data = yaml.safe_load(
            (PROFILE / "contacts.example.yaml").read_text(encoding="utf-8")
        )
        for entry in data["contacts"]:
            for field in ("id", "name", "company", "relationship", "channel"):
                assert field in entry, f"contact {entry.get('id')} missing {field}"
            assert entry["channel"] in {"linkedin", "email"}
            if entry["channel"] == "email":
                assert entry.get("email"), f"{entry['id']} is email-channel with no email"

    def test_contacts_relationships_match_the_outreach_channels(self):
        """/outreach has exactly three channel rubrics."""
        data = yaml.safe_load(
            (PROFILE / "contacts.example.yaml").read_text(encoding="utf-8")
        )
        used = {e["relationship"] for e in data["contacts"]}
        assert used <= {"recruiter", "referral", "alumni", "other"}


class TestExampleLaneIdsResolve:
    """The example_* lanes name SKILL ids in their Skills layouts. Those ids
    resolved to nothing before skills_master.example.md existed, so the shipped
    configuration could not render a Skills section at all."""

    @staticmethod
    def _lane_skill_ids() -> set[str]:
        ids: set[str] = set()
        for path in (PROFILE / "verticals").glob("example_*/*.md"):
            ids |= set(
                re.findall(
                    r"SKILL-[A-Z0-9]+(?:-[A-Z0-9]+)*",
                    path.read_text(encoding="utf-8"),
                )
            )
        return ids

    @staticmethod
    def _defined_skill_ids() -> set[str]:
        text = (PROFILE / "skills_master.example.md").read_text(encoding="utf-8")
        return set(re.findall(r"^## (SKILL-\S+)", text, re.M))

    @staticmethod
    def _defined_bullet_ids() -> set[str]:
        text = (PROFILE / "bullets.example.md").read_text(encoding="utf-8")
        return set(re.findall(r"^## (B-\S+)", text, re.M))

    def test_every_lane_skill_id_is_defined(self):
        undefined = self._lane_skill_ids() - self._defined_skill_ids()
        assert not undefined, (
            f"example lanes reference SKILL ids nothing defines: {sorted(undefined)}"
        )

    def test_no_orphan_skills_in_the_template(self):
        orphans = self._defined_skill_ids() - self._lane_skill_ids()
        assert not orphans, (
            f"skills_master.example.md defines ids no lane renders: {sorted(orphans)}"
        )

    def test_skill_evidence_cites_bullets_that_exist(self):
        text = (PROFILE / "skills_master.example.md").read_text(encoding="utf-8")
        cited = set(re.findall(r"B-[A-Z]+-\d+", text))
        missing = cited - self._defined_bullet_ids()
        assert not missing, f"evidence cites undefined bullet ids: {sorted(missing)}"

    def test_every_bullet_is_cited_by_some_skill(self):
        text = (PROFILE / "skills_master.example.md").read_text(encoding="utf-8")
        cited = set(re.findall(r"B-[A-Z]+-\d+", text))
        assert not (self._defined_bullet_ids() - cited)

    def test_vertical_lean_values_are_example_lane_names(self):
        text = (PROFILE / "skills_master.example.md").read_text(encoding="utf-8")
        lanes = {p.name for p in (PROFILE / "verticals").glob("example_*")}
        for block in re.findall(r"^vertical_lean: \[(.*)\]$", text, re.M):
            for value in (v.strip() for v in block.split(",") if v.strip()):
                assert value in lanes, f"vertical_lean {value!r} is not a lane dir"


class TestExampleEntrySchemas:
    BULLET_KEYS = ("source:", "canonical:", "tags:", "evidence:", "allowable_synonyms:")
    SKILL_KEYS = ("name:", "category:", "evidence:", "allowable_synonyms:", "vertical_lean:")

    @staticmethod
    def _entries(name: str) -> list[str]:
        text = (PROFILE / name).read_text(encoding="utf-8")
        return re.split(r"^## ", text, flags=re.M)[1:]

    def test_every_bullet_entry_has_every_key(self):
        for entry in self._entries("bullets.example.md"):
            entry_id = entry.split("\n", 1)[0].strip()
            for key in self.BULLET_KEYS:
                assert key in entry, f"bullet {entry_id} missing {key}"

    def test_every_skill_entry_has_every_key(self):
        for entry in self._entries("skills_master.example.md"):
            entry_id = entry.split("\n", 1)[0].strip()
            for key in self.SKILL_KEYS:
                assert key in entry, f"skill {entry_id} missing {key}"

    def test_bullet_ids_follow_the_documented_shape(self):
        for entry in self._entries("bullets.example.md"):
            entry_id = entry.split("\n", 1)[0].strip()
            assert re.fullmatch(r"B-[A-Z]+-\d{2}", entry_id), (
                f"bullet id {entry_id!r} does not match B-<CTX>-NN"
            )


def test_committed_de_ai_rules_ships_the_diction_gate_off():
    """de_ai_rules.yaml is a committed default, so a fresh clone runs with
    whatever it says. src/lint.py defaults the gate to False when the key is
    absent; the shipped file must not override that to True, or a new user's
    canonical bullets skip Tier-2 diction linting before they have read them."""
    rules = yaml.safe_load((PROFILE / "de_ai_rules.yaml").read_text(encoding="utf-8"))
    assert rules["bullets_diction_pass_completed"] is False
