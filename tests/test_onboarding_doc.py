"""`/onboarding` makes numeric claims about itself — question count, minutes per
step, position lines, the step a cross-reference points at. Nothing checked them,
so a renumber could leave half the file on the old scheme and still read fine.

These tests parse `.claude/commands/onboarding.md` (and the `pass-a`/`pass-b`
contract it shares with `new-vertical.md`) and assert the file agrees with
itself. Nothing here imports `src/` or runs a command.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ONBOARDING = REPO_ROOT / ".claude" / "commands" / "onboarding.md"
NEW_VERTICAL = REPO_ROOT / ".claude" / "commands" / "new-vertical.md"

WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# `## Step 3 — scoring starts (~3 min, 2 questions)`
STEP_HEADING_RE = re.compile(
    r"^## Step (?P<num>\d+) — (?P<title>[^(]+)\((?P<meta>[^)]*)\)\s*$", re.M)
# `- `Step 1 of 5 · ~18 min left · you'll see real scored jobs at step 4.``
POSITION_LINE_RE = re.compile(
    r"`Step (?P<num>\d+) of (?P<total>\d+) · (?P<left>[^·]+?) · (?P<tail>[^`]+)`")
# `**Question 5**` and the batched `**questions 3 and 4**`
QUESTION_MARK_RE = re.compile(
    r"\*\*Questions? (?P<a>\d+)(?: and (?P<b>\d+))?\*\*", re.I)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing command file: {path}"
    return path.read_text(encoding="utf-8")


def _word_or_int(token: str, where: str) -> int:
    key = token.strip().lower()
    if key.isdigit():
        return int(key)
    assert key in WORD_NUMBERS, f"{where}: cannot read {token!r} as a number"
    return WORD_NUMBERS[key]


@pytest.fixture(scope="module")
def doc() -> str:
    return _read(ONBOARDING)


@pytest.fixture(scope="module")
def new_vertical_doc() -> str:
    return _read(NEW_VERTICAL)


@pytest.fixture(scope="module")
def frontmatter(doc) -> str:
    parts = doc.split("---\n", 2)
    assert len(parts) >= 3 and parts[0] == "", "onboarding.md has no frontmatter block"
    return parts[1]


@pytest.fixture(scope="module")
def description(frontmatter) -> str:
    match = re.search(r"^description: (.+)$", frontmatter, re.M)
    assert match, "onboarding.md frontmatter has no single-line `description:`"
    return match.group(1)


@pytest.fixture(scope="module")
def opening(doc) -> str:
    """The paragraph under the `# /onboarding` title that restates the pitch."""
    match = re.search(r"^# /onboarding[^\n]*\n\n(.+?)\n\n", doc, re.S | re.M)
    assert match, "onboarding.md has no opening paragraph under its `# /onboarding` title"
    return match.group(1)


@pytest.fixture(scope="module")
def steps(doc) -> dict:
    """{step number: {minutes, questions, title, body}} for every `## Step N`."""
    matches = list(STEP_HEADING_RE.finditer(doc))
    assert matches, "no `## Step N — title (~N min, N questions)` headings found"
    out = {}
    for i, m in enumerate(matches):
        num = int(m.group("num"))
        meta = m.group("meta")
        mins = re.search(r"~(\d+) min\b", meta)
        qs = re.search(r"(\d+) questions?\b", meta)
        assert mins, f"step {num} heading has no `~N min`: ({meta})"
        assert qs, f"step {num} heading has no `N question(s)`: ({meta})"
        end = doc.find("\n## ", m.end())
        if end == -1:
            end = doc.find("\n# ", m.end())
        assert num not in out, f"duplicate `## Step {num}` heading"
        out[num] = {
            "minutes": int(mins.group(1)),
            "questions": int(qs.group(1)),
            "title": m.group("title").strip(),
            "unnumbered": "unnumbered" in meta,
            "body": doc[m.end():end if end != -1 else len(doc)],
        }
    return out


@pytest.fixture(scope="module")
def numbered_steps(steps) -> dict:
    return {n: s for n, s in steps.items() if not s["unnumbered"]}


class TestQuestionCount:
    """The frontmatter, the contract bullet and the step headings must agree."""

    def test_headings_sum_to_the_stated_total(self, doc, description, opening, steps):
        contract = re.search(r"\*\*(\w+) questions, total\.\*\*", doc)
        assert contract, "no `**N questions, total.**` bullet in the binding contract"
        stated = _word_or_int(contract.group(1), "contract bullet")

        summed = sum(s["questions"] for s in steps.values())
        per_step = {n: s["questions"] for n, s in sorted(steps.items())}
        assert summed == stated, (
            f"step headings sum to {summed} questions ({per_step}) but the contract "
            f"bullet says {stated}")

        for label, text in (("frontmatter description", description),
                            ("opening paragraph", opening)):
            m = re.search(r"(\w+) questions instead of", text)
            assert m, f"{label} no longer states a question count"
            claimed = _word_or_int(m.group(1), label)
            assert claimed == stated, (
                f"{label} claims {claimed} questions, contract bullet says {stated}")

    def test_contract_per_step_breakdown_matches_the_headings(self, doc, steps):
        """The contract spells out which steps carry questions ("Step 1: four")."""
        bullet = re.search(r"\*\*\w+ questions, total\.\*\*(.+?)\n-", doc, re.S)
        assert bullet, "cannot isolate the questions-total contract bullet"
        claimed = {int(n): _word_or_int(w, "contract per-step breakdown")
                   for n, w in re.findall(r"Step (\d+): (\w+)", bullet.group(1))}
        assert claimed, "the contract bullet names no per-step question counts"
        for num, count in claimed.items():
            assert num in steps, f"contract names Step {num}, which has no heading"
            assert steps[num]["questions"] == count, (
                f"contract says Step {num} asks {count} question(s); its heading says "
                f"{steps[num]['questions']}")
        for num, step in steps.items():
            if num not in claimed:
                assert step["questions"] == 0, (
                    f"step {num} heading claims {step['questions']} question(s) but the "
                    f"contract bullet does not list it")


class TestQuestionNumbering:
    def test_numbers_run_1_to_total_with_no_gaps_or_duplicates(self, doc, steps):
        found = []
        for m in QUESTION_MARK_RE.finditer(doc):
            found.append(int(m.group("a")))
            if m.group("b"):
                found.append(int(m.group("b")))
        total = sum(s["questions"] for s in steps.values())
        assert sorted(found) == list(range(1, total + 1)), (
            f"`**Question N**` markers are {sorted(found)}; expected a clean 1..{total} "
            f"to match the {total} questions the step headings promise")

    def test_each_step_marks_the_questions_its_heading_claims(self, steps):
        for num, step in sorted(steps.items()):
            marked = []
            for m in QUESTION_MARK_RE.finditer(step["body"]):
                marked.append(int(m.group("a")))
                if m.group("b"):
                    marked.append(int(m.group("b")))
            assert len(marked) == step["questions"], (
                f"step {num} heading claims {step['questions']} question(s) but its body "
                f"marks {len(marked)} ({marked})")


class TestPositionLines:
    def test_one_verbatim_line_per_numbered_step(self, doc, numbered_steps):
        lines = list(POSITION_LINE_RE.finditer(doc))
        nums = [int(m.group("num")) for m in lines]
        expected = sorted(numbered_steps)
        assert nums == expected, (
            f"position lines cover steps {nums}; the numbered `## Step N` headings are "
            f"{expected} — one verbatim line per numbered step, in order")
        for m in lines:
            total = int(m.group("total"))
            assert total == len(numbered_steps), (
                f"`Step {m.group('num')} of {total}` but there are "
                f"{len(numbered_steps)} numbered steps")

    def test_minutes_left_never_grows(self, doc):
        left = []
        for m in POSITION_LINE_RE.finditer(doc):
            mins = re.search(r"~(\d+) min left", m.group("left"))
            left.append((int(m.group("num")), int(mins.group(1)) if mins else 0))
        for (a_num, a), (b_num, b) in zip(left, left[1:]):
            assert b <= a, (
                f"position line for step {b_num} claims {b} min left, more than step "
                f"{a_num}'s {a} — a renumber left a stale figure")

    def test_minutes_left_equals_the_time_still_ahead(self, doc, steps, numbered_steps):
        """`~N min left` must equal total minutes minus the steps already done."""
        total = sum(s["minutes"] for s in steps.values())
        for m in POSITION_LINE_RE.finditer(doc):
            num = int(m.group("num"))
            mins = re.search(r"~(\d+) min left", m.group("left"))
            if not mins:            # the last line says "done", not a figure
                continue
            spent = sum(s["minutes"] for n, s in steps.items() if n < num)
            assert int(mins.group(1)) == total - spent, (
                f"step {num}'s position line says ~{mins.group(1)} min left; the step "
                f"headings put {total - spent} min after step {num - 1} "
                f"(total {total}, spent {spent})")


class TestTiming:
    def test_step_minutes_sum_to_the_advertised_total(self, description, opening, steps):
        summed = sum(s["minutes"] for s in steps.values())
        per_step = {n: s["minutes"] for n, s in sorted(steps.items())}
        for label, text, pattern in (
            ("frontmatter description", description, r"about (\d+) minutes"),
            ("opening paragraph", opening, r"~(\d+) minutes"),
        ):
            m = re.search(pattern, text)
            assert m, f"{label} no longer states a total duration (/{pattern}/)"
            claimed = int(m.group(1))
            assert abs(claimed - summed) <= 1, (
                f"{label} claims {claimed} minutes; the step headings sum to {summed} "
                f"({per_step})")

    def test_first_scored_jobs_land_at_the_advertised_minute(self, description, doc, steps):
        """The description promises scored jobs "by minute N"; the prose promises
        them "at step M". Minute N must be when step M starts."""
        minute = re.search(r"by minute (\d+)", description)
        step = re.search(r"real scored (?:jobs|postings)[^.`]*?at step (\d+)", doc)
        assert minute and step, (
            "onboarding.md no longer promises scored jobs at a minute (frontmatter) and "
            "a step (prose); drop this test or restate the claim")
        target = int(step.group(1))
        start = sum(s["minutes"] for n, s in steps.items() if n < target)
        assert int(minute.group(1)) == start, (
            f"description promises scored jobs by minute {minute.group(1)}, but step "
            f"{target} starts at minute {start} per the step headings")

    def test_the_step_that_shows_scored_jobs_is_named_consistently(
            self, description, opening, doc):
        claims = []
        for label, text in (("frontmatter description", description),
                            ("opening paragraph", opening),
                            ("step 1 position line", doc)):
            for m in re.finditer(r"real scored (?:jobs|postings)[^.`]*?at step (\d+)", text):
                claims.append((label, int(m.group(1))))
        assert claims, "no 'real scored jobs at step N' claim left to check"
        targets = {n for _, n in claims}
        assert len(targets) == 1, f"'real scored jobs at step N' disagrees: {claims}"


class TestPassAPassBContract:
    MARKER = "profile/verticals/<name>/.pass_a_only"

    @staticmethod
    def _markers(text: str) -> set:
        return {p.replace("<lane>", "<name>")
                for p in re.findall(r"profile/verticals/<\w+>/\.pass_a_only", text)}

    def test_onboarding_invokes_both_passes(self, doc):
        for token in ("pass-a", "pass-b"):
            assert re.search(rf"/new-vertical [^`\n]*{token}", doc), (
                f"onboarding.md no longer invokes `/new-vertical ... {token}`")

    def test_new_vertical_documents_both_tokens(self, new_vertical_doc):
        for token in ("pass-a", "pass-b"):
            assert f"`{token}`" in new_vertical_doc, (
                f"new-vertical.md does not document the `{token}` mode token that "
                f"onboarding.md passes it")

    def test_marker_path_is_identical_in_both_files(self, doc, new_vertical_doc):
        here, there = self._markers(doc), self._markers(new_vertical_doc)
        assert here == {self.MARKER}, (
            f"onboarding.md's pass-a marker path is {here or 'absent'}, expected "
            f"{{{self.MARKER!r}}} (<lane>/<name> placeholders are interchangeable)")
        assert there == here, (
            f"marker path drifted: onboarding.md says {here}, new-vertical.md says "
            f"{there}")


class TestCrossReferences:
    def test_no_reference_to_a_step_above_the_last_one(self, doc, numbered_steps):
        last = max(numbered_steps)
        stale = {int(n) for n in re.findall(r"\bstep\s+(\d+)", doc, re.I)
                 if int(n) > last}
        assert not stale, (
            f"onboarding.md references step(s) {sorted(stale)}, but the file only "
            f"defines steps up to {last} — left over from a renumber")

    def test_new_vertical_points_at_the_step_that_authors_bullets(
            self, doc, new_vertical_doc, steps):
        """new-vertical.md stops and sends the user to `/onboarding step N` when
        bullets.md is still the example. N must be the step that authors it."""
        pointer = re.search(r"/onboarding step (\d+)", new_vertical_doc)
        assert pointer, "new-vertical.md no longer points at an `/onboarding step N`"
        authoring = [n for n, s in steps.items()
                     if "draft both yourself" in s["body"] and "bullets.md" in s["body"]]
        assert len(authoring) == 1, (
            "cannot identify the step that authors bullets.md (looked for a step body "
            f"containing 'draft both yourself' and 'bullets.md'; matched {authoring})")
        assert int(pointer.group(1)) == authoring[0], (
            f"new-vertical.md sends the user to /onboarding step {pointer.group(1)}, but "
            f"bullets.md is authored in step {authoring[0]}")


class TestCitedCode:
    """`src/` citations in the prose go stale silently; profile/ is user data and
    absent on a fresh clone, so only src/ paths are checked."""

    def test_cited_src_paths_and_symbols_exist(self, doc):
        cites = re.findall(r"src/[\w/]+\.py(?::(\w+))?", doc)
        paths = re.findall(r"(src/[\w/]+\.py)(?::(\w+))?", doc)
        assert paths, "no src/ path cited in onboarding.md — drop this test if intended"
        assert len(cites) == len(paths)
        for path, symbol in paths:
            target = REPO_ROOT / path
            assert target.is_file(), f"onboarding.md cites {path}, which does not exist"
            if symbol:
                source = target.read_text(encoding="utf-8")
                assert re.search(rf"^\s*(def|class|{symbol}\s*=)\s*{symbol}\b|"
                                 rf"^{symbol}\s*=", source, re.M), (
                    f"onboarding.md cites {path}:{symbol}, but {symbol} is not defined "
                    f"in that file")
