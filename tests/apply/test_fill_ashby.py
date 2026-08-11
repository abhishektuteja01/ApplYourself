"""The Ashby browser driver, run against the markup live boards render.

Playwright is not what is under test — the selectors are. Ashby shares no
markup with Greenhouse, so every inherited selector is a fresh assumption, and
the way one gets caught is by resolving it against a real captured DOM rather
than by asserting the string it happens to produce. `MiniPage` therefore
resolves the small CSS subset this driver uses (`fill.py`'s Ashby class) over
`form_ashby_widgets.html`.

The subset is deliberately tiny. A selector this fake cannot parse is a
selector that should not be in the driver: anything relying on Ashby's
CSS-module hashes is out of bounds, since those change on every deploy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from lxml import html as lxml_html

from src.apply import fill as F
from src.apply.fill import FillError, FillResult, fill_plan
from src.apply.plan import FieldPlan, Plan

FIXTURES = Path(__file__).parent / "fixtures"


# --- a very small CSS subset, resolved over lxml -----------------------------

_SIMPLE = re.compile(
    r'^(?P<tag>[a-z]+|\*)?'
    r'(?P<attrs>(?:\[[^\]]+\])*)$'
)
_ATTR = re.compile(r'\[(?P<name>[\w-]+)(?:(?P<op>[*]?=)"(?P<value>[^"]*)")?\]')


def _xpath_step(simple: str) -> str:
    m = _SIMPLE.match(simple.strip())
    if not m:
        raise ValueError(f"MiniPage cannot parse {simple!r}")
    tag = m.group("tag") or "*"
    predicates = []
    for attr in _ATTR.finditer(m.group("attrs") or ""):
        name, op, value = attr.group("name"), attr.group("op"), attr.group("value")
        if op is None:
            predicates.append(f"@{name}")
        elif op == "=":
            predicates.append(f'@{name}="{value}"')
        else:                                   # *= substring
            predicates.append(f'contains(@{name},"{value}")')
    return tag + "".join(f"[{p}]" for p in predicates)


def css_to_xpath(selector: str) -> str:
    """Descendant combinators and comma groups only — no child, no sibling."""
    branches = []
    for branch in selector.split(","):
        steps = [_xpath_step(s) for s in branch.strip().split() if s]
        branches.append(".//" + "//".join(steps))
    return " | ".join(branches)


class MiniLocator:
    def __init__(self, page, nodes):
        self.page, self.nodes = page, list(nodes)

    def count(self):
        return len(self.nodes)

    def nth(self, i):
        return MiniLocator(self.page, self.nodes[i:i + 1])

    @property
    def first(self):
        return MiniLocator(self.page, self.nodes[:1])

    def locator(self, selector):
        found = []
        for node in self.nodes:
            found.extend(node.xpath(css_to_xpath(selector)))
        return MiniLocator(self.page, found)

    # --- reads ---
    def get_attribute(self, name):
        return self.nodes[0].get(name) if self.nodes else None

    def inner_text(self):
        return self.nodes[0].text_content() if self.nodes else ""

    def input_value(self, timeout=None):
        if not self.nodes:
            raise AssertionError("input_value on nothing")
        node = self.nodes[0]
        if node.tag not in ("input", "textarea", "select"):
            raise ValueError("not an input")           # what Playwright does
        if node.get("type") in ("checkbox", "radio") and node.get("value") is None:
            return "on"      # the HTML default, and what Playwright returns
        return node.get("value", "")

    # --- writes, recorded ---
    def _id(self):
        """What a recorded interaction is attributed to. A control with no id
        of its own (a chevron, a toggle button) is attributed to the field it
        sits in, so a test can tell which question was answered."""
        node = self.nodes[0]
        own = node.get("name") or node.get("id")
        if own:
            return own
        while node is not None:
            path = node.get("data-field-path")
            if path:
                return path
            node = node.getparent()
        return self.nodes[0].tag

    def click(self):
        self.page.calls.append(("click", self._id(), self.inner_text().strip()))

    def check(self):
        self.page.calls.append(("check", self._id()))

    def fill(self, value):
        self.page.calls.append(("fill", self._id(), value))
        self.nodes[0].set("value", value)

    def type(self, value, delay=None):
        self.page.calls.append(("type", self._id(), value))
        self.nodes[0].set("value", value)

    def set_input_files(self, path):
        self.page.calls.append(("attach", self._id(), path))


class MiniPage:
    def __init__(self, fixture="form_ashby_widgets.html"):
        self.doc = lxml_html.fromstring((FIXTURES / fixture).read_text(encoding="utf-8"))
        self.calls: list[tuple] = []

    def locator(self, selector):
        return MiniLocator(self, self.doc.xpath(css_to_xpath(selector)))

    def wait_for_selector(self, selector, timeout=None):
        if not self.doc.xpath(css_to_xpath(selector)):
            raise TimeoutError(selector)

    def goto(self, url, wait_until=None):
        self.calls.append(("goto", url))

    def wait_for_load_state(self, state, timeout=None):
        pass


def driver(fixture="form_ashby_widgets.html"):
    page = MiniPage(fixture)
    return F.AshbyBrowserDriver(page), page


COUNTRY = "00000010-0000-0000-0000-000000000010"
YESNO = "00000011-0000-0000-0000-000000000011"
CHECKS = "00000012-0000-0000-0000-000000000012"
RADIOS = "00000013-0000-0000-0000-000000000013"


class TestMiniPageItself:
    """If the fake is wrong the rest of the file proves nothing."""

    def test_it_resolves_an_attribute_selector(self):
        page = MiniPage()
        assert page.locator(f'[data-field-path="{YESNO}"]').count() == 1

    def test_it_resolves_a_descendant_and_a_substring_match(self):
        page = MiniPage()
        assert page.locator('[role="listbox"] [role="option"]').count() == 3
        assert page.locator('button[class*="_active_"]').count() == 1

    def test_it_refuses_a_selector_shape_the_driver_must_not_use(self):
        with pytest.raises(ValueError):
            css_to_xpath("div > span")


class TestKindIsReadFromTheDom:
    """The API's type does not name the widget: one `select` renders as a
    radio group at a few options and as a combobox at many. The threshold is
    Ashby's, and undocumented, so the plan's kind is only a fallback."""

    @pytest.mark.parametrize("field_id,planned,expected", [
        (COUNTRY, "select", "react_select"),        # enumerated, rendered as a combobox
        ("_systemfield_location", "combobox", "react_select"),  # server-backed
        (RADIOS, "select", "radio_group"),          # same API type, few options
        (CHECKS, "select", "checkbox_group"),
        (YESNO, "yesno", "yesno"),
        ("_systemfield_name", "text", "text"),
    ])
    def test_the_widget_on_the_page_wins(self, field_id, planned, expected):
        d, _ = driver()
        assert d.resolve_kind(field_id, planned) == expected

    def test_a_field_that_is_not_rendered_keeps_its_planned_kind(self):
        """A conditional question absent from the DOM must fail the ordinary
        way later, not be silently reinterpreted here."""
        d, _ = driver()
        assert d.resolve_kind("no-such-field", "select") == "select"

    def test_greenhouse_and_lever_are_left_alone(self):
        """The hook is Ashby-only: a board that renders what it declared must
        see the planned kind come back untouched."""
        for cls in (F.BrowserDriver, F.LeverBrowserDriver):
            assert cls(MiniPage()).resolve_kind(COUNTRY, "react_select") == "react_select"


class TestCombobox:
    def test_options_are_read_by_clicking_the_chevron_not_the_input(self):
        """Clicking the input leaves aria-expanded false and opens nothing —
        the single reason this widget was unreadable before."""
        d, page = driver()
        options = d.open_options(COUNTRY)
        assert options == ("United States", "Canada", "Ireland")
        clicked = [c for c in page.calls if c[0] == "click"]
        assert len(clicked) == 1 and clicked[0][2] == ""      # the chevron, no text

    def test_options_come_from_the_document_level_listbox(self):
        """The panel is floated out of the field it belongs to, so scoping the
        option lookup to the field entry would find nothing."""
        d, _ = driver()
        assert d.visible_options() == ("United States", "Canada", "Ireland")

    def test_an_option_is_matched_on_its_visible_text(self):
        d, page = driver()
        assert d.click_option("canada") is True                # case-insensitive
        assert ("click", ":r4:", "Canada") in page.calls

    def test_an_absent_option_is_a_miss_not_a_wrong_click(self):
        d, page = driver()
        assert d.click_option("Belgium") is False
        assert [c for c in page.calls if c[0] == "click"] == []

    def test_expansion_is_read_off_the_combobox_input(self):
        d, _ = driver()
        assert d.is_expanded(COUNTRY) is True
        assert d.is_expanded("_systemfield_location") is False

    def test_a_field_with_no_combobox_is_not_expanded(self):
        d, _ = driver()
        assert d.is_expanded(YESNO) is False

    def test_typing_goes_into_the_combobox_input(self):
        """The combobox carries neither id nor name, so it is only reachable
        through its field entry."""
        d, page = driver()
        d.type_into("_systemfield_location", "New York")
        assert ("type", "_systemfield_location", "New York") in [
            (c[0], "_systemfield_location", c[2]) for c in page.calls if c[0] == "type"
        ]

    def test_the_chosen_value_reads_back_from_the_input(self):
        d, _ = driver()
        d.type_into(COUNTRY, "United States")
        assert d.selected_label(COUNTRY) == "United States"


class TestYesNoToggle:
    def test_the_matching_button_is_clicked(self):
        d, page = driver()
        d.set_yesno(YESNO, "Yes")
        assert ("click", YESNO, "Yes") in page.calls

    def test_the_label_match_is_case_insensitive(self):
        d, page = driver()
        d.set_yesno(YESNO, "no")
        assert ("click", YESNO, "No") in page.calls

    def test_an_unknown_label_raises_rather_than_clicking_something(self):
        d, page = driver()
        with pytest.raises(FillError, match="no yes/no button"):
            d.set_yesno(YESNO, "Maybe")
        assert [c for c in page.calls if c[0] == "click"] == []

    def test_the_answer_is_read_from_the_active_class(self):
        """The fixture has "No" chosen. The hidden checkbox cannot express
        this — it reads false both here and when untouched — so the class is
        the only usable read."""
        d, _ = driver()
        assert d.selected_label(YESNO) == "No"

    def test_a_toggle_nobody_has_touched_reads_empty(self):
        """No active class means no answer, which parks. The read fails in the
        safe direction: it can never invent a selection that is not there."""
        page = MiniPage()
        for node in page.doc.xpath('//button[contains(@class,"_active_")]'):
            node.set("class", "_container_pjyt6_1 _option_1svni_32 ")
        assert F.AshbyBrowserDriver(page).selected_label(YESNO) == ""


class TestGroups:
    def test_a_checkbox_option_is_matched_on_its_name(self):
        d, page = driver()
        d.check_group_option(CHECKS, "Gizmo Operations")
        assert ("check", "Gizmo Operations") in page.calls

    def test_a_radio_option_is_matched_on_its_label_text(self):
        """Radios carry the group name, not the option text, so only the
        `<label for>` distinguishes them."""
        d, page = driver()
        d.check_radio_group(RADIOS, "No, always on site")
        assert [c for c in page.calls if c[0] == "check"] == [
            ("check", f"00000001-0000-0000-0000-000000000001_{RADIOS}")
        ]

    def test_an_absent_option_raises_rather_than_ticking_a_neighbour(self):
        d, page = driver()
        with pytest.raises(FillError, match="no checkbox labelled"):
            d.check_group_option(CHECKS, "Cog Assembly")
        assert [c for c in page.calls if c[0] == "check"] == []

    def test_a_radio_miss_names_the_control_type(self):
        d, _ = driver()
        with pytest.raises(FillError, match="no radio labelled"):
            d.check_radio_group(RADIOS, "Sometimes")


class TestPageLoad:
    def test_it_waits_for_a_field_entry_not_a_form(self):
        """Ashby renders no <form> element at all, so the inherited wait would
        time out on every board."""
        d, page = driver()
        d.goto("https://jobs.ashbyhq.com/gasketworks/0000/application")
        assert ("goto", "https://jobs.ashbyhq.com/gasketworks/0000/application") in page.calls

    def test_a_page_with_no_fields_is_not_ready(self):
        page = MiniPage()
        for node in page.doc.xpath("//*[@data-field-path]"):
            node.getparent().remove(node)
        with pytest.raises(TimeoutError):
            F.AshbyBrowserDriver(page).goto("https://jobs.ashbyhq.com/x/1/application")


class TestValueOf:
    def test_a_text_field_reads_its_own_value(self):
        d, page = driver()
        d.fill_text("_systemfield_name", "Dana Rivera")
        assert d.value_of("_systemfield_name") == "Dana Rivera"

    def test_a_toggle_falls_back_to_the_chosen_button(self):
        """The display-only checkbox reads as the HTML default "on" whether or
        not it is checked (measured live), so reading it would report every
        untouched toggle as prefilled and answer none of them correctly."""
        d, _ = driver()
        assert d.value_of(YESNO) == "No"

    def test_an_open_listbox_option_is_not_mistaken_for_an_answer(self):
        """The highlighted option in an open dropdown also carries `_active_`.
        It is a div, and nobody chose it — reading it as the toggle's answer
        would report a question answered that was never touched."""
        page = MiniPage()
        for node in page.doc.xpath('//button[contains(@class,"_active_")]'):
            node.set("class", "_container_pjyt6_1 _option_1svni_32 ")
        assert page.locator('[role="option"][class*="_active_"]').count() == 1
        assert F.AshbyBrowserDriver(page).selected_label(YESNO) == ""

    def test_an_untouched_toggle_is_not_reported_as_prefilled(self):
        page = MiniPage()
        for node in page.doc.xpath('//button[contains(@class,"_active_")]'):
            node.set("class", "_container_pjyt6_1 _option_1svni_32 ")
        assert F.AshbyBrowserDriver(page).value_of(YESNO) == ""

    def test_an_absent_field_reads_empty(self):
        d, _ = driver()
        assert d.value_of("no-such-field") == ""


class TestYesNoThroughTheFillSequence:
    """The shared dispatcher had no branch for this kind — a yes/no field fell
    through to "type text into it"."""

    def _plan(self, **kw):
        base = dict(id=YESNO, name=YESNO, label="A question", kind="yesno",
                    section="questions", required=True, multi=False,
                    value="No", tier="B0")
        base.update(kw)
        return Plan(
            job_id="a1b2c3d4", board="gasketworks", token="1",
            form_url="https://jobs.ashbyhq.com/gasketworks/0000/application",
            company="Gasket Works", title="Widget Engineer", out_dir=Path("/tmp"),
            fields=(FieldPlan(**base),), files=(), unmapped=(), draftable=(),
            skipped=(), submit_selector=None, submit_disabled=False,
        )

    def test_a_yesno_field_clicks_the_button_and_is_a_fill(self):
        d, page = driver()
        result = fill_plan(self._plan(), d)
        assert result.ok is True
        assert ("click", YESNO, "No") in page.calls
        assert [o.action for o in result.outcomes] == ["filled"]

    def test_a_toggle_that_did_not_take_is_a_failure_not_a_fill(self):
        """Same discipline as a react-select: the widget is asserted after the
        write, so a click that landed nowhere cannot be reported as filled."""
        d, _ = driver()
        result = fill_plan(self._plan(value="Yes"), d)   # fixture stays on "No"
        assert result.ok is False
        assert YESNO in result.failures[0]

    def test_the_base_driver_has_no_toggle_at_all(self):
        """Greenhouse and Lever never render one; a plan that asks for it on
        those boards is a bug, not something to improvise."""
        with pytest.raises(FillError, match="no yes/no toggle"):
            F.BrowserDriver(MiniPage()).set_yesno("q1", "Yes")


class TestAshbyStaysManualApply:
    """Registering the driver is a separate decision with its own blast radius
    — the shortlist and the run report both read this."""

    def test_there_is_still_no_registered_ashby_driver(self):
        assert F.has_driver("ashby") is False
