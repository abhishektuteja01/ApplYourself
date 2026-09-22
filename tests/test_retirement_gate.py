"""An adversarial sweep of the one decision that can delete a board.

`decide_verdict` is the only producer of a retireable verdict, so rather than
trust the cascade's own tests, this enumerates every combination of the facts
an `Outcome` can carry and asserts that `moved` and `gone` are reachable only
under their full precondition set. A regression here silently prunes live
companies out of the universe, which is not recoverable from the CSV alone.
"""
import itertools

import pytest

import scripts.slug_resolvers as R

STATUSES = ("active", "stale", "empty", "dead", "error")


def _outcome(status, input_live, found, domain, web_checked, errors):
    return R.Outcome(
        old_status=status,
        input_live=input_live,
        found=found,
        domain=domain,
        web_checked=web_checked,
        errors=list(errors),
    )


def _all_outcomes():
    live_hi = R.Candidate(ats="ashby", slug="new", jobs=7, tier="t2", confidence=R.HIGH)
    live_lo = R.Candidate(ats="ashby", slug="new", jobs=7, tier="t2", confidence=R.LOW)
    for status, input_live, found, domain, web, errs in itertools.product(
        STATUSES, (False, True), (None, live_hi, live_lo),
        ("", "acme.com"), (False, True), ((), ("403",)),
    ):
        yield _outcome(status, input_live, found, domain, web, errs)


def test_every_outcome_yields_a_known_verdict():
    for o in _all_outcomes():
        verdict, confidence = R.decide_verdict(o)
        assert verdict in R.VERDICTS
        assert confidence in (R.HIGH, R.LOW)


def test_retirement_is_reachable_only_under_its_full_preconditions():
    retired = [o for o in _all_outcomes() if R.is_retireable(R.decide_verdict(o)[0])]
    assert retired, "the sweep must actually exercise the retireable paths"

    for o in retired:
        verdict = R.decide_verdict(o)[0]
        # The old board must have been *observed* not serving. A board the
        # audit saw answering is at most `dual`, never retired.
        assert not o.input_live
        assert o.old_status not in R.LIVE_STATUSES

        if verdict == R.GONE:
            # `gone` is an inference from absence, so any failure poisons it:
            # a 403 means we did not learn anything.
            assert not o.errors, f"gone retired despite errors {o.errors}"
            assert o.found is None
            assert not o.input_live
            # A live website is never evidence the company is gone.
            assert not o.domain
            # Skipping tier 3 is not evidence of absence.
            assert o.web_checked
            # Only a 404 lane; `empty` is a board that exists.
            assert o.old_status in R.NOT_FOUND_STATUSES
        else:
            assert verdict == R.MOVED
            # `moved` is a positive confirmation, so an unrelated probe
            # failure does not invalidate it -- but it does require a live
            # replacement, which `gone` by definition never has.
            # A move needs a confirmed live replacement, at high confidence,
            # and the old board must not still be serving (that is `dual`).
            assert o.found is not None and o.found.jobs > 0
            assert o.found.confidence == R.HIGH
            assert not o.input_live


@pytest.mark.parametrize("status", [s for s in STATUSES if s not in R.NOT_FOUND_STATUSES])
def test_a_board_that_exists_is_never_gone(status):
    """`empty` is a 200 with zero postings -- the board is there."""
    o = _outcome(status, False, None, "", True, ())
    assert R.decide_verdict(o)[0] != R.GONE


def test_no_web_pass_cannot_retire_anything():
    """--no-web leaves web_checked False on every row, so a run without the
    website tier must be incapable of proposing a single retirement."""
    for o in _all_outcomes():
        if o.web_checked:
            continue
        assert R.decide_verdict(o)[0] != R.GONE


def test_retireable_set_is_closed():
    assert R.RETIREABLE == frozenset({R.MOVED, R.GONE})
    for v in R.VERDICTS:
        assert R.is_retireable(v) is (v in (R.MOVED, R.GONE))
    for v in (R.OK, R.DUAL, R.ALIVE_NO_BOARD, R.REVIEW, R.ERROR):
        assert not R.is_retireable(v)


def test_only_a_confirmed_404_can_be_retired():
    """Every retirement, by either verdict, requires the audit to have seen a
    404. `empty`/`stale`/`active` boards all answered 200 and exist, so the
    pass can never drop a board that is still serving."""
    for o in _all_outcomes():
        if R.is_retireable(R.decide_verdict(o)[0]):
            assert o.old_status in R.NOT_FOUND_STATUSES


def test_an_empty_board_with_a_replacement_is_dual_not_moved():
    """The one case that would retire a live board. An empty board yields no
    rows anyway, so keeping it is nearly free; dropping it is irreversible."""
    found = R.Candidate(ats="ashby", slug="new", jobs=9, tier="t2", confidence=R.HIGH)
    o = _outcome("empty", False, found, "", True, ())
    verdict, _ = R.decide_verdict(o)
    assert verdict == R.DUAL
    assert not R.is_retireable(verdict)
    # The find is still reported, so the apply pass can add the new board.
    row = dict(zip(R.COLUMNS, R.result_row(
        {"ats": "greenhouse", "slug": "old", "name": "Acme"}, o)))
    assert row["new_ats"] == "ashby" and row["new_slug"] == "new"


def test_a_dead_board_with_a_replacement_is_moved():
    found = R.Candidate(ats="ashby", slug="new", jobs=9, tier="t2", confidence=R.HIGH)
    o = _outcome("dead", False, found, "", True, ())
    assert R.decide_verdict(o)[0] == R.MOVED


# --- the offsite retirement route -------------------------------------------
# A match on a platform we do not scrape retires the old row and proposes no
# addition. It is its own verdict and its own set, so the sweep above -- which
# never sets `offsite` -- still describes the `moved`/`gone` routes exactly.


def _offsite_outcomes():
    off_hi = R.Offsite(ats="bamboohr", slug="acmelabs", confidence=R.HIGH)
    off_lo = R.Offsite(ats="bamboohr", slug="loop", confidence=R.LOW)
    for status, input_live, off, domain, web, errs in itertools.product(
        STATUSES, (False, True), (off_hi, off_lo),
        ("", "acme.com"), (False, True), ((), ("403",)),
    ):
        out = _outcome(status, input_live, None, domain, web, errs)
        out.offsite = off
        out.name_match = True
        yield out


def test_moved_offsite_is_gated_exactly_as_moved():
    for o in _offsite_outcomes():
        if R.decide_verdict(o)[0] != R.MOVED_OFFSITE:
            continue
        assert o.old_status in R.NOT_FOUND_STATUSES
        assert o.old_status not in R.LIVE_STATUSES
        assert not o.input_live
        assert not o.errors
        assert o.offsite.confidence == R.HIGH


def test_the_offsite_route_is_reachable_and_retires_the_old_row():
    verdicts = {R.decide_verdict(o)[0] for o in _offsite_outcomes()}
    assert R.MOVED_OFFSITE in verdicts, "the sweep must exercise the offsite route"
    assert R.retires_old_row(R.MOVED_OFFSITE)
    # It proposes no addition, so it is not in the `moved`/`gone` set.
    assert not R.is_retireable(R.MOVED_OFFSITE)


def test_the_offsite_set_is_closed():
    assert R.RETIREABLE_OFFSITE == frozenset({R.MOVED_OFFSITE})
    assert R.RETIRES_OLD_ROW == frozenset({R.MOVED, R.GONE, R.MOVED_OFFSITE})
    for v in R.VERDICTS:
        assert R.retires_old_row(v) is (v in (R.MOVED, R.GONE, R.MOVED_OFFSITE))


def test_a_match_on_any_platform_can_never_be_gone():
    """Positive evidence the company exists outranks every absence finding."""
    for o in _offsite_outcomes():
        assert R.decide_verdict(o)[0] != R.GONE
    for o in _all_outcomes():
        o.name_match = True
        assert R.decide_verdict(o)[0] != R.GONE


def test_an_offsite_match_never_reports_a_posting_count():
    """Nothing was probed, so a 0 in `new_jobs` would read as an empty board."""
    o = _outcome("dead", False, None, "", True, ())
    o.offsite = R.Offsite(ats="bamboohr", slug="acmelabs", confidence=R.HIGH)
    o.name_match = True
    row = dict(zip(R.COLUMNS, R.result_row(
        {"ats": "greenhouse", "slug": "old", "name": "Acme Labs"}, o)))
    assert row["verdict"] == R.MOVED_OFFSITE
    assert (row["new_ats"], row["new_slug"]) == ("bamboohr", "acmelabs")
    assert row["new_jobs"] == ""


# --- the dead-end route: retirement authorized by a search log --------------
# Policy set by the user: a 404 board is retired even when the company is
# demonstrably alive, because a 404 board is a dead end for scraping either
# way -- but only once the search for a replacement is provably exhausted.
# `error` rows (403 WAF, 429 throttle, 5xx outage) are never retired, because
# those mean we learned nothing about the board at all.

import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location(
    "apply_reconcile", _Path(__file__).resolve().parents[1] / "scripts" / "apply_reconcile.py")
_apply = _ilu.module_from_spec(_spec)
_sys.modules["apply_reconcile"] = _apply
_spec.loader.exec_module(_apply)


class _Holder:
    def __init__(self):
        self.path = _Path("greenhouse.csv")
        self.retired = []
        self._active = {}

    def active(self, slug):
        return self._active.get(slug, "1")

    def retire(self, slug):
        self.retired.append(slug)


class _Universe:
    def __init__(self, holder=None):
        self._holder = holder

    def holder(self, ats, slug):
        return self._holder

    def file(self, ats, local):
        return self._holder


def _row(**kw):
    base = {"ats": "greenhouse", "slug": "s", "name": "Acme", "old_status": "dead",
            "verdict": R.ALIVE_NO_BOARD, "new_ats": "", "new_slug": "",
            "confidence": R.HIGH}
    base.update(kw)
    return base


def _plan(row, exhausted, selected=(_apply.DEAD_END,)):
    holder = _Holder()
    changes = _plan.last = _apply.plan_row(row, _Universe(holder), frozenset(selected),
                                           exhausted)
    return changes, holder


def test_dead_end_needs_a_search_log_row():
    """Without proof the search ran, nothing is retired."""
    changes, holder = _plan(_row(), exhausted=set())
    assert [c.action for c in changes] == [_apply.ACT_REFUSED]
    assert "search-log" in changes[0].detail
    assert holder.retired == []


def test_dead_end_retires_once_the_search_is_exhausted():
    changes, holder = _plan(_row(), exhausted={("greenhouse", "s")})
    assert [c.action for c in changes] == [_apply.ACT_RETIRE]
    assert holder.retired == ["s"]


@pytest.mark.parametrize("status", ["active", "stale", "empty", "error"])
def test_dead_end_refuses_anything_that_is_not_a_confirmed_404(status):
    """A 403/429/5xx board was never checked; an empty board answered 200."""
    changes, holder = _plan(_row(old_status=status), exhausted={("greenhouse", "s")})
    assert [c.action for c in changes] == [_apply.ACT_REFUSED]
    assert "not a confirmed 404" in changes[0].detail
    assert holder.retired == []


def test_dead_end_is_not_reachable_without_selecting_it():
    changes, holder = _plan(_row(), exhausted={("greenhouse", "s")},
                            selected=(R.MOVED,))
    assert changes == []
    assert holder.retired == []


def test_gone_also_needs_the_search_log_once_one_is_given():
    """Uniform rule: no 404 board is retired without proof the search ran."""
    row = _row(verdict=R.GONE)
    changes, holder = _plan(row, exhausted=set(), selected=(R.GONE,))
    assert [c.action for c in changes] == [_apply.ACT_REFUSED]
    assert holder.retired == []

    changes, holder = _plan(row, exhausted={("greenhouse", "s")}, selected=(R.GONE,))
    assert [c.action for c in changes] == [_apply.ACT_RETIRE]
    assert holder.retired == ["s"]


def test_a_search_log_row_cannot_add_a_board():
    """The log only ever authorizes a retirement. Additions come from the
    proposal, so a `found` row in the log must not become an addition."""
    log = "ats,slug,outcome,new_ats,new_slug\ngreenhouse,s,found,ashby,newslug\n"
    import io
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(log)
        path = fh.name
    assert _apply.load_search_log(path) == set()


# --- the wider candidate list, as the gate sees it --------------------------
# Board slugs mangle the company name -- a segment suffix glued on, truncation
# at the board's width limit, `dot` spelled out -- so the resolver now guesses
# stripped stems and bounded prefixes too. A short prefix can land on an
# unrelated parked domain, which costs one spurious `alive_no_board`. These
# cases pin that direction: a resolving candidate always blocks retirement, and
# widening the guess list cannot open a new route into `gone`.

MANGLED = [
    ("pjtpartnersprofessionals", "pjtpartners.com"),
    ("evolutionparkingguestservices", "evolutionparking.com"),
    ("roundpointmortgageservicingcor", "roundpointmortgage.com"),
    ("halodotscience", "halo.science"),
    ("centerforhumancompatibleartifi", "humancompatible.ai"),
    ("shrapnelstudio", "shrapnel.com"),
    ("zscalerredcanary", "zscaler.com"),
]


@pytest.mark.parametrize("slug,domain", MANGLED)
def test_a_prefix_or_stripped_candidate_that_resolves_is_never_gone(slug, domain):
    o = _outcome("dead", False, None, domain, True, ())
    verdict, _ = R.decide_verdict(o)
    assert verdict == R.ALIVE_NO_BOARD
    assert not R.retires_old_row(verdict)


@pytest.mark.parametrize("slug,domain", MANGLED)
def test_the_resolver_can_actually_reach_that_domain(slug, domain):
    """The gate is only worth anything if the candidate is generated at all."""
    assert domain in R.domain_candidates(slug, slug)


def test_a_spurious_prefix_hit_is_alive_no_board_never_a_retirement():
    """The accepted tradeoff, stated as an assertion: a parked domain that
    resolves buys a wrong bucket, never a wrong retirement."""
    o = _outcome("dead", False, None, "parked.com", True, ())
    assert R.decide_verdict(o) == (R.ALIVE_NO_BOARD, R.LOW)
    assert not R.retires_old_row(R.ALIVE_NO_BOARD)


def test_widening_the_candidate_list_did_not_widen_the_gate():
    """`gone` still needs all of: a confirmed 404, tier 3 having run, no
    errors, no find, no name match and nothing resolving."""
    for o in _all_outcomes():
        if R.decide_verdict(o)[0] != R.GONE:
            continue
        assert o.old_status in R.NOT_FOUND_STATUSES
        assert o.web_checked and not o.errors
        assert o.found is None and not o.domain and not o.name_match
