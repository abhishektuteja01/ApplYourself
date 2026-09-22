"""scripts/reconcile_slugs.py: the driver, with the network layer replaced.

Every test here stubs `probe_board` and the web client, so the suite never
touches a board API or a company website. What is under test is the cascade's
order and its stopping rule, the per-row budget, the CSV writer's resume
behaviour, and the guard that keeps this pass report-only.
"""
from __future__ import annotations

import csv
import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

from src.ats_http import CareersError

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "reconcile_slugs.py"


def _load():
    spec = importlib.util.spec_from_file_location("reconcile_slugs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def M():
    return _load()


@pytest.fixture
def R(M):
    return M.R


# --- stubs ------------------------------------------------------------------

def stub_probes(M, monkeypatch, table: dict, default=None):
    """`table` maps (ats, slug) -> Probe. Anything unlisted 404s."""
    calls = []

    def fake(ats, slug, pacer=None):
        calls.append((ats, slug))
        return table.get((ats, slug), default or M.Probe(not_found=True))

    monkeypatch.setattr(M, "probe_board", fake)
    return calls


def stub_web(M, pages: dict, dns: dict | None = None):
    """`pages` maps url -> html. A missing url does not answer.

    `dns` maps domain -> R.DNS_* and overrides the default, which is that a
    domain resolves exactly when `pages` carries a URL on it -- so a test that
    only cares about page content needs no DNS table.
    """
    fetched = []
    looked_up = []
    hosts = {u.split("/")[2] for u in pages if u.startswith("https://")}

    class FakeWeb:
        def __init__(self, limit=M.MAX_WEB_FETCHES):
            self.limit, self.used = limit, 0

        def resolves(self, domain):
            looked_up.append(domain)
            if dns and domain in dns:
                return dns[domain]
            return M.R.DNS_OK if domain in hosts else M.R.DNS_NXDOMAIN

        def get(self, url):
            if self.used >= self.limit:
                return None
            self.used += 1
            fetched.append(url)
            html = pages.get(url)
            return (url, html) if html is not None else None

    FakeWeb.fetched = fetched
    FakeWeb.looked_up = looked_up
    return FakeWeb


def ctx_for(M, companies=None, pages=None, no_web=False, dns=None):
    return M.Context(companies=companies or {}, pacer=None, no_web=no_web,
                     web_factory=stub_web(M, pages or {}, dns))


def row(ats="greenhouse", slug="acme", name="Acme Labs", status="dead"):
    return {"ats": ats, "slug": slug, "name": name, "status": status}


def field(M, result, name):
    return result[M.R.COLUMNS.index(name)]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- tier 1 -----------------------------------------------------------------

class TestTier1LeverCase:
    def test_a_casing_retry_finds_the_live_board(self, M, monkeypatch):
        calls = stub_probes(M, monkeypatch, {("lever", "Fliff"): M.Probe(jobs=20)})
        result = M.resolve_row(row("lever", "fliff", "Fliff"), ctx_for(M, no_web=True))
        assert field(M, result, "verdict") == M.R.MOVED
        assert (field(M, result, "new_ats"), field(M, result, "new_slug")) == ("lever", "Fliff")
        assert field(M, result, "new_jobs") == 20
        assert ("lever", "Fliff") in calls

    def test_the_companies_csv_casing_is_tried_first(self, M, monkeypatch):
        companies = {"fliff": [M.R.CompanyRow("lever", "Fliff", "FLIFF", "")]}
        calls = stub_probes(M, monkeypatch, {("lever", "FLIFF"): M.Probe(jobs=3)})
        M.resolve_row(row("lever", "fliff", "Fliff"), ctx_for(M, companies, no_web=True))
        assert calls[0] == ("lever", "FLIFF")

    def test_only_lever_and_only_a_404(self, M, monkeypatch):
        calls = stub_probes(M, monkeypatch, {})
        M.resolve_row(row("greenhouse", "acme", "Acme Labs"), ctx_for(M, no_web=True))
        assert not any(a == "lever" for a, _ in calls)
        calls.clear()
        M.resolve_row(row("lever", "fliff", "Fliff", status="empty"),
                      ctx_for(M, no_web=True))
        assert calls == []

    def test_a_board_with_zero_postings_is_not_a_confirmation(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {("lever", "Fliff"): M.Probe(jobs=0)})
        result = M.resolve_row(row("lever", "fliff", "Fliff"), ctx_for(M))
        assert field(M, result, "verdict") == M.R.GONE
        assert field(M, result, "new_slug") == ""


# --- tier 2 -----------------------------------------------------------------

class TestTier2NameMatch:
    def test_a_distinct_name_match_is_moved(self, M, monkeypatch):
        companies = {"paciolan": [M.R.CompanyRow("ashby", "Paciolan", "paciolan", "")]}
        stub_probes(M, monkeypatch, {("ashby", "paciolan"): M.Probe(jobs=4)})
        result = M.resolve_row(row("greenhouse", "paciolan", "Paciolan"),
                               ctx_for(M, companies, no_web=True))
        assert field(M, result, "verdict") == M.R.MOVED
        assert field(M, result, "confidence") == M.R.HIGH

    def test_a_workday_candidate_comes_from_its_url_column(self, M, monkeypatch):
        companies = {"paciolan": [M.R.CompanyRow(
            "workday", "Paciolan", "paciolan",
            "https://learfield.wd5.myworkdayjobs.com/en-US/Paciolan")]}
        calls = stub_probes(M, monkeypatch,
                            {("workday", "learfield|wd5|Paciolan"): M.Probe(jobs=9)})
        result = M.resolve_row(row("greenhouse", "paciolan", "Paciolan"),
                               ctx_for(M, companies, no_web=True))
        assert ("workday", "learfield|wd5|Paciolan") in calls
        assert field(M, result, "new_slug") == "learfield|wd5|Paciolan"
        assert field(M, result, "acquirer") == "learfield"

    def test_a_workday_candidate_with_an_unparseable_url_is_skipped(self, M, monkeypatch):
        companies = {"paciolan": [M.R.CompanyRow("workday", "Paciolan", "paciolan", "")]}
        calls = stub_probes(M, monkeypatch, {})
        M.resolve_row(row("greenhouse", "paciolan", "Paciolan"),
                      ctx_for(M, companies, no_web=True))
        assert calls == []

    def test_an_ambiguous_name_is_review_not_moved(self, M, monkeypatch):
        """`loop` collides across unrelated companies; the find is real but it
        cannot be trusted to retire the old slug."""
        companies = {"loop": [M.R.CompanyRow("ashby", "Loop", "loop-inc", "")]}
        stub_probes(M, monkeypatch, {("ashby", "loop-inc"): M.Probe(jobs=11)})
        result = M.resolve_row(row("greenhouse", "loop", "Loop"),
                               ctx_for(M, companies, no_web=True))
        assert field(M, result, "verdict") == M.R.REVIEW
        assert field(M, result, "confidence") == M.R.LOW
        assert field(M, result, "new_slug") == "loop-inc"
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_the_input_board_is_never_its_own_candidate(self, M, monkeypatch):
        companies = {"acme labs": [M.R.CompanyRow("greenhouse", "Acme Labs", "ACME", "")]}
        calls = stub_probes(M, monkeypatch, {})
        M.resolve_row(row("greenhouse", "acme", "Acme Labs", status="active"),
                      ctx_for(M, companies, no_web=True))
        assert calls == []

    def test_a_live_input_board_plus_another_live_board_is_dual(self, M, monkeypatch):
        companies = {"acme labs": [M.R.CompanyRow("ashby", "Acme Labs", "acmelabs", "")]}
        stub_probes(M, monkeypatch, {("ashby", "acmelabs"): M.Probe(jobs=6)})
        result = M.resolve_row(row("greenhouse", "acme", "Acme Labs", status="active"),
                               ctx_for(M, companies, no_web=True))
        assert field(M, result, "verdict") == M.R.DUAL
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_an_active_row_with_no_migration_is_ok(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(status="active"), ctx_for(M, no_web=True))
        assert field(M, result, "verdict") == M.R.OK


# --- tier 2b: the platforms we do not scrape --------------------------------

def offsite(M, ats="bamboohr", name="Acme Labs", slug="acmelabs"):
    return {M.R.normalize_name(name): [M.R.CompanyRow(ats, name, slug, "")]}


class TestTier2OffsiteNameMatch:
    def test_a_dead_row_matched_offsite_is_moved_offsite(self, M, monkeypatch):
        calls = stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(), ctx_for(M, offsite(M)))
        assert field(M, result, "verdict") == M.R.MOVED_OFFSITE
        assert (field(M, result, "new_ats"), field(M, result, "new_slug")) == \
            ("bamboohr", "acmelabs")
        # Nothing was probed, so there is no posting count to report.
        assert field(M, result, "new_jobs") == ""
        assert calls == []
        assert M.R.retires_old_row(field(M, result, "verdict"))
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_it_stops_before_the_web_tier(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        web = stub_web(M, {"https://acmelabs.com": "<html>x</html>"})
        ctx = M.Context(companies=offsite(M), pacer=None, web_factory=web)
        result = M.resolve_row(row(), ctx)
        assert web.looked_up == [] and web.fetched == []
        assert field(M, result, "verdict") == M.R.MOVED_OFFSITE
        assert field(M, result, "domain") == ""

    def test_the_evidence_names_the_destination_platform(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(), ctx_for(M, offsite(M)))
        assert "tier2_offsite:bamboohr/acmelabs" in field(M, result, "evidence")
        assert "name_match" in field(M, result, "evidence")

    def test_a_supported_match_is_preferred_over_an_offsite_one(self, M, monkeypatch):
        companies = {"acme labs": [
            M.R.CompanyRow("bamboohr", "Acme Labs", "acmelabs", ""),
            M.R.CompanyRow("ashby", "Acme Labs", "acme-labs", "")]}
        calls = stub_probes(M, monkeypatch, {("ashby", "acme-labs"): M.Probe(jobs=4)})
        result = M.resolve_row(row(), ctx_for(M, companies, no_web=True))
        assert calls == [("ashby", "acme-labs")]
        assert field(M, result, "verdict") == M.R.MOVED
        assert field(M, result, "new_ats") == "ashby"

    def test_an_unsupported_platform_is_never_probed(self, M, monkeypatch):
        """There is no client for these APIs; probing would only manufacture an
        error that poisons the row."""
        companies = {"acme labs": [
            M.R.CompanyRow(a, "Acme Labs", "acmelabs", "")
            for a in ("bamboohr", "workable", "smartrecruiters", "rippling")]}
        calls = stub_probes(M, monkeypatch, {})
        M.resolve_row(row(), ctx_for(M, companies, no_web=True))
        assert calls == []

    def test_a_dead_supported_probe_falls_through_to_the_offsite_match(self, M, monkeypatch):
        companies = {"acme labs": [
            M.R.CompanyRow("ashby", "Acme Labs", "acme-labs", ""),
            M.R.CompanyRow("bamboohr", "Acme Labs", "acmelabs", "")]}
        calls = stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(), ctx_for(M, companies, no_web=True))
        assert calls == [("ashby", "acme-labs")]
        assert field(M, result, "verdict") == M.R.MOVED_OFFSITE

    @pytest.mark.parametrize("status", ["empty", "stale", "active"])
    def test_a_board_that_answered_200_is_never_retired_offsite(self, M, monkeypatch,
                                                                status):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(status=status), ctx_for(M, offsite(M)))
        verdict = field(M, result, "verdict")
        assert verdict != M.R.MOVED_OFFSITE
        assert not M.R.retires_old_row(verdict)

    def test_an_ambiguous_name_is_review_not_retired(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("greenhouse", "loop", "Loop"),
                               ctx_for(M, offsite(M, name="Loop", slug="loop-inc")))
        assert field(M, result, "verdict") == M.R.REVIEW
        assert field(M, result, "confidence") == M.R.LOW
        assert field(M, result, "new_slug") == "loop-inc"
        assert not M.R.retires_old_row(field(M, result, "verdict"))

    def test_the_input_board_itself_is_not_evidence_of_anything(self, M, monkeypatch):
        """The audited slug came out of the same universe the dump did, so a
        self-match must not block `gone`."""
        companies = {"acme labs": [M.R.CompanyRow("greenhouse", "Acme Labs", "acme", "")]}
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(), ctx_for(M, companies))
        assert field(M, result, "verdict") == M.R.GONE
        assert "name_match" not in field(M, result, "evidence")


class TestAnyMatchIsNeverGone:
    """A row matched on any platform has positive evidence the company exists.
    Whatever the web tier would have concluded, it cannot read `gone`."""

    @pytest.mark.parametrize("ats", ["bamboohr", "workable", "smartrecruiters",
                                     "rippling", "gem", "personio", "join_com"])
    def test_an_offsite_match_is_never_gone(self, M, monkeypatch, ats):
        stub_probes(M, monkeypatch, {})
        # No pages and no DNS: exactly the fact pattern that reads `gone`.
        result = M.resolve_row(row(), ctx_for(M, offsite(M, ats=ats)))
        assert field(M, result, "verdict") != M.R.GONE
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_a_supported_match_that_404s_everywhere_is_never_gone(self, M, monkeypatch):
        companies = {"acme labs": [M.R.CompanyRow("ashby", "Acme Labs", "acme-labs", "")]}
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(), ctx_for(M, companies))
        assert field(M, result, "verdict") == M.R.REVIEW
        assert not M.R.retires_old_row(field(M, result, "verdict"))

    def test_the_same_row_with_no_match_at_all_is_gone(self, M, monkeypatch):
        """The control: the guard is what changed the verdict, not the stubs."""
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(), ctx_for(M))
        assert field(M, result, "verdict") == M.R.GONE


# --- tier 3 -----------------------------------------------------------------

class TestTier3Domain:
    def test_an_ats_link_on_the_homepage(self, M, monkeypatch):
        pages = {"https://acmelabs.com": '<a href="https://boards.greenhouse.io/acmelabs2">J</a>'}
        stub_probes(M, monkeypatch, {("greenhouse", "acmelabs2"): M.Probe(jobs=7)})
        result = M.resolve_row(row("lever", "acme", "Acme Labs"), ctx_for(M, pages=pages))
        assert field(M, result, "verdict") == M.R.MOVED
        assert field(M, result, "domain") == "acmelabs.com"
        assert "tier3_site" in field(M, result, "evidence")

    def test_a_guessed_careers_path(self, M, monkeypatch):
        pages = {"https://acmelabs.com": "<html>nothing</html>",
                 "https://acmelabs.com/careers": "jobs.ashbyhq.com/acmelabs"}
        stub_probes(M, monkeypatch, {("ashby", "acmelabs"): M.Probe(jobs=2)})
        result = M.resolve_row(row("lever", "acme", "Acme Labs"), ctx_for(M, pages=pages))
        assert field(M, result, "new_ats") == "ashby"

    def test_one_hop_from_the_homepage(self, M, monkeypatch):
        """The lift over the guessed paths: many careers pages are a click
        deeper, under a URL we would never have guessed."""
        pages = {"https://acmelabs.com": '<a href="/company/work/openings">Open jobs</a>',
                 "https://acmelabs.com/company/work/openings":
                     "https://jobs.lever.co/AcmeLabs"}
        stub_probes(M, monkeypatch, {("lever", "AcmeLabs"): M.Probe(jobs=5)})
        result = M.resolve_row(row("greenhouse", "acme", "Acme Labs"),
                               ctx_for(M, pages=pages))
        assert (field(M, result, "new_ats"), field(M, result, "new_slug")) == \
            ("lever", "AcmeLabs")

    def test_a_site_with_no_recognized_board_is_alive_no_board(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(
            row("lever", "acme", "Acme Labs"),
            ctx_for(M, pages={"https://acmelabs.com": "<html>about us</html>"}))
        assert field(M, result, "verdict") == M.R.ALIVE_NO_BOARD
        assert field(M, result, "domain") == "acmelabs.com"
        assert "site_up_no_board" in field(M, result, "evidence")
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_an_unsupported_platform_is_recorded_and_never_probed(self, M, monkeypatch):
        pages = {"https://acmelabs.com": "https://apply.workable.com/acmelabs/"}
        calls = stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("greenhouse", "acme", "Acme Labs"), ctx_for(M, pages=pages))
        assert "unsupported_ats:workable=acmelabs" in field(M, result, "evidence")
        assert calls == []
        assert field(M, result, "verdict") == M.R.ALIVE_NO_BOARD

    def test_no_domain_resolves_and_the_board_404s_is_gone(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("lever", "acme", "Acme Labs"), ctx_for(M))
        assert field(M, result, "verdict") == M.R.GONE
        assert "no_domain" in field(M, result, "evidence")
        assert M.R.is_retireable(field(M, result, "verdict"))

    def test_no_web_skips_the_tier_entirely(self, M, monkeypatch):
        pages = {"https://acmelabs.com": "https://boards.greenhouse.io/acmelabs2"}
        stub_probes(M, monkeypatch, {("greenhouse", "acmelabs2"): M.Probe(jobs=7)})
        web = stub_web(M, pages)
        ctx = M.Context(companies={}, pacer=None, no_web=True, web_factory=web)
        result = M.resolve_row(row("lever", "acme", "Acme Labs"), ctx)
        assert web.fetched == []
        # Not `gone`: tier 3 never ran, so absence was never established.
        assert field(M, result, "verdict") == M.R.REVIEW

    def test_the_site_pointing_back_at_the_input_board_is_not_a_move(self, M, monkeypatch):
        pages = {"https://acmelabs.com": "https://boards.greenhouse.io/acme"}
        stub_probes(M, monkeypatch, {("greenhouse", "acme"): M.Probe(jobs=7)})
        result = M.resolve_row(row("greenhouse", "acme", "Acme Labs"),
                               ctx_for(M, pages=pages))
        assert field(M, result, "verdict") == M.R.ALIVE_NO_BOARD

    def test_an_acquisition_found_on_the_site(self, M, monkeypatch):
        pages = {"https://adcolony.com":
                 "https://digitalturbine.wd501.myworkdayjobs.com/en-US/DT_Careers"}
        stub_probes(M, monkeypatch,
                    {("workday", "digitalturbine|wd501|DT_Careers"): M.Probe(jobs=40)})
        result = M.resolve_row(row("lever", "adcolony", "AdColony"), ctx_for(M, pages=pages))
        assert field(M, result, "acquirer") == "digitalturbine"
        assert field(M, result, "verdict") == M.R.MOVED


# --- errors and budgets -----------------------------------------------------

class TestErrorsAndBudget:
    @pytest.mark.parametrize("status", [403, 429, 500])
    def test_a_non_404_failure_is_error_and_never_retires(self, M, monkeypatch, status):
        companies = {"acme labs": [M.R.CompanyRow("ashby", "Acme Labs", "acmelabs", "")]}
        stub_probes(M, monkeypatch,
                    {("ashby", "acmelabs"): M.Probe(error=f"ashby/acmelabs:{status}")})
        result = M.resolve_row(row(), ctx_for(M, companies, no_web=True))
        assert field(M, result, "verdict") == M.R.ERROR
        assert f":{status}" in field(M, result, "evidence")
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_probe_board_maps_a_404_and_a_wall_differently(self, M, monkeypatch):
        from src.ats_http import CareersError

        def raiser(status):
            def f(url, *a, **k):
                raise CareersError("x", status=status)
            return f

        monkeypatch.setattr(M, "fetch_json", raiser(404))
        assert M.probe_board("ashby", "acme").not_found
        monkeypatch.setattr(M, "fetch_json", raiser(403))
        p = M.probe_board("ashby", "acme")
        assert p.error and not p.not_found and not p.live

    def test_probe_board_counts_a_good_payload(self, M, monkeypatch):
        monkeypatch.setattr(M, "fetch_json", lambda url, *a, **k: {"jobs": [{}, {}]})
        assert M.probe_board("greenhouse", "acme") == M.Probe(jobs=2)

    def test_probe_board_never_raises(self, M, monkeypatch):
        def boom(url, *a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(M, "fetch_json", boom)
        assert "RuntimeError" in M.probe_board("greenhouse", "acme").error

    def test_a_malformed_200_is_an_error_not_an_empty_board(self, M, monkeypatch):
        monkeypatch.setattr(M, "fetch_json", lambda url, *a, **k: "<html>")
        assert "malformed" in M.probe_board("greenhouse", "acme").error

    def test_the_probe_budget_caps_one_row(self, M, monkeypatch):
        companies = {"acme labs": [
            M.R.CompanyRow("ashby", "Acme Labs", f"acme{i}", "") for i in range(50)]}
        calls = stub_probes(M, monkeypatch, {})
        M.resolve_row(row(), ctx_for(M, companies, no_web=True))
        assert len(calls) == M.MAX_BOARD_PROBES

    def test_exhausting_the_budget_reads_as_error_never_gone(self, M, monkeypatch):
        companies = {"acme labs": [
            M.R.CompanyRow("ashby", "Acme Labs", f"acme{i}", "") for i in range(50)]}
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row(), ctx_for(M, companies, no_web=True))
        assert field(M, result, "verdict") == M.R.ERROR
        assert "budget_exhausted" in field(M, result, "evidence")

    def test_one_bad_row_does_not_end_the_pass(self, M, monkeypatch):
        monkeypatch.setattr(M, "tier2_name_match",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
        result = M.resolve_row(row(), ctx_for(M, no_web=True))
        assert field(M, result, "verdict") == M.R.ERROR
        assert "row:ValueError" in field(M, result, "evidence")

    def test_a_web_client_budget_stops_fetching(self, M, monkeypatch):
        pages = {f"https://acmelabs.com{p}": "<html>x</html>"
                 for p in ("",) + M.R.CAREERS_PATHS}
        stub_probes(M, monkeypatch, {})
        web = stub_web(M, pages)
        ctx = M.Context(companies={}, pacer=None, web_factory=lambda: web(limit=2))
        M.resolve_row(row("lever", "acme", "Acme Labs"), ctx)
        assert len(web.fetched) <= 2


# --- the pacer --------------------------------------------------------------

class TestPacer:
    def test_it_spaces_consecutive_calls_on_one_lane(self, M):
        slept = []
        pacer = M.Pacer({"ashby": 0.05})
        original = M.time.sleep
        try:
            M.time.sleep = slept.append
            pacer.wait("ashby")
            pacer.wait("ashby")
        finally:
            M.time.sleep = original
        assert slept and 0 < slept[0] <= 0.05

    def test_an_unpaced_lane_does_not_sleep(self, M):
        pacer = M.Pacer({})
        pacer.wait("ashby")  # no KeyError, no sleep


# --- CSV I/O ----------------------------------------------------------------

class TestWriterAndResume:
    def test_the_header_is_written_once_and_rows_flush(self, M, tmp_path):
        out = tmp_path / "r.csv"
        w = M.Writer(out, existing=False)
        w.write(["greenhouse", "acme"] + [""] * (len(M.R.COLUMNS) - 2))
        assert out.read_text(encoding="utf-8").splitlines()[0] == ",".join(M.R.COLUMNS)
        w.close()

    def test_appending_does_not_repeat_the_header(self, M, tmp_path):
        out = tmp_path / "r.csv"
        M.Writer(out, existing=False).close()
        w = M.Writer(out, existing=True)
        w.write(["lever", "acme"] + [""] * (len(M.R.COLUMNS) - 2))
        w.close()
        lines = out.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("ats,") and len(lines) == 2

    def test_load_done_reads_the_pairs_already_written(self, M, tmp_path):
        out = tmp_path / "r.csv"
        out.write_text(",".join(M.R.COLUMNS) + "\ngreenhouse,acme,Acme,dead,gone,,,,,high,,\n",
                       encoding="utf-8")
        assert M.load_done(out) == {("greenhouse", "acme")}
        assert M.load_done(tmp_path / "missing.csv") == set()


class TestLoadAudit:
    @pytest.fixture
    def audit(self, tmp_path):
        p = tmp_path / "audit.csv"
        p.write_text(
            "ats,slug,name,status,postings,newest_posting,http,note\n"
            "greenhouse,a,A,dead,0,,404,\n"
            "greenhouse,b,B,active,9,,200,\n"
            "lever,c,C,error,0,,403,\n"
            "lever,d,D,empty,0,,200,\n"
            "ashby,e,E,stale,3,,200,\n"
            "greenhouse,,Blank,dead,0,,404,\n",
            encoding="utf-8")
        return p

    def test_splits_by_lane_and_filters_by_status(self, M, audit):
        work = M.load_audit(audit, ("greenhouse", "lever"), ("dead", "empty"), set(), None)
        assert [r["slug"] for r in work["greenhouse"]] == ["a"]
        assert [r["slug"] for r in work["lever"]] == ["d"]

    def test_error_rows_are_excluded_by_the_default_statuses(self, M, audit):
        work = M.load_audit(audit, ("lever",), M.DEFAULT_STATUSES, set(), None)
        assert [r["slug"] for r in work["lever"]] == ["d"]

    def test_active_rows_are_in_by_default_so_migrations_are_caught(self, M, audit):
        work = M.load_audit(audit, ("greenhouse",), M.DEFAULT_STATUSES, set(), None)
        assert {r["slug"] for r in work["greenhouse"]} == {"a", "b"}

    def test_done_pairs_and_blank_slugs_are_skipped(self, M, audit):
        work = M.load_audit(audit, ("greenhouse",), M.DEFAULT_STATUSES,
                            {("greenhouse", "a")}, None)
        assert [r["slug"] for r in work["greenhouse"]] == ["b"]

    def test_limit_is_per_lane(self, M, audit):
        work = M.load_audit(audit, ("greenhouse", "ashby"), M.DEFAULT_STATUSES, set(), 1)
        assert len(work["greenhouse"]) == 1 and len(work["ashby"]) == 1


# --- main -------------------------------------------------------------------

@pytest.fixture
def cli(M, monkeypatch, tmp_path):
    """main() with config, companies.csv and the network all stubbed out."""
    class Src:
        pacing_seconds = 0.0

    monkeypatch.setattr(M, "load_config", lambda: type(
        "C", (), {"sources": {a: Src() for a in M.R.SUPPORTED_ATS}})())
    monkeypatch.setattr(M, "pacing_floor", lambda ats: 0.0)
    companies = tmp_path / "companies.csv"
    companies.write_text("ats,name,slug,url\n", encoding="utf-8")
    monkeypatch.setattr(M, "COMPANIES_CSV", companies)
    monkeypatch.setattr(M, "MAIN_COMPANIES_CSV", companies)
    monkeypatch.setattr(M, "OUT_DIR", tmp_path)
    return tmp_path


class TestMain:
    def _audit(self, tmp_path):
        p = tmp_path / "audit.csv"
        p.write_text(
            "ats,slug,name,status,postings,newest_posting,http,note\n"
            "greenhouse,acme,Acme Labs,dead,0,,404,\n"
            "lever,fliff,Fliff,dead,0,,404,\n",
            encoding="utf-8")
        return p

    def test_writes_a_well_formed_csv(self, M, monkeypatch, cli):
        stub_probes(M, monkeypatch, {("lever", "Fliff"): M.Probe(jobs=20)})
        out = cli / "out.csv"
        assert M.main(["--audit", str(self._audit(cli)), "--no-web", "--out", str(out)]) == 0
        rows = _read(out)
        assert {r["ats"] for r in rows} == {"greenhouse", "lever"}
        assert set(rows[0]) == set(M.R.COLUMNS)
        by_slug = {r["slug"]: r for r in rows}
        assert by_slug["fliff"]["verdict"] == M.R.MOVED
        assert by_slug["fliff"]["new_slug"] == "Fliff"
        assert by_slug["acme"]["verdict"] == M.R.REVIEW

    def test_resume_skips_what_is_already_written(self, M, monkeypatch, cli):
        stub_probes(M, monkeypatch, {})
        out = cli / "out.csv"
        out.write_text(",".join(M.R.COLUMNS) +
                       "\ngreenhouse,acme,Acme Labs,dead,gone,,,,,high,,\n", encoding="utf-8")
        assert M.main(["--audit", str(self._audit(cli)), "--no-web",
                       "--resume", "--out", str(out)]) == 0
        assert [r["slug"] for r in _read(out)] == ["acme", "fliff"]

    def test_the_ats_filter(self, M, monkeypatch, cli):
        stub_probes(M, monkeypatch, {})
        out = cli / "out.csv"
        assert M.main(["--audit", str(self._audit(cli)), "--no-web",
                       "--ats", "lever", "--out", str(out)]) == 0
        assert [r["ats"] for r in _read(out)] == ["lever"]

    def test_a_missing_audit_csv_exits_one(self, M, cli):
        assert M.main(["--audit", str(cli / "nope.csv")]) == 1

    def test_an_unknown_status_exits_one(self, M, cli):
        assert M.main(["--audit", str(self._audit(cli)), "--status", "zombie"]) == 1

    def test_nothing_to_do_is_not_an_error(self, M, cli):
        assert M.main(["--audit", str(self._audit(cli)), "--status", "stale"]) == 0

    @pytest.mark.parametrize("bad", ["data/universe/greenhouse.csv",
                                     "pipeline/abc/state.yaml",
                                     "profile/companies.yaml"])
    def test_it_refuses_to_write_outside_a_report(self, M, cli, bad):
        """Report-only: the apply pass is a separate script."""
        assert M.main(["--audit", str(self._audit(cli)), "--out", bad]) == 1

    def test_the_summary_counts_only_retireable_verdicts(self, M, capsys):
        counts = {"greenhouse": {M.R.GONE: 3, M.R.MOVED: 2, M.R.ALIVE_NO_BOARD: 5,
                                 M.R.ERROR: 4, M.R.OK: 1}}
        M.print_summary(("greenhouse",), counts, "x.csv")
        assert "5 rows would propose active=0" in capsys.readouterr().out

    def test_the_summary_counts_moved_offsite_as_a_retirement(self, M, capsys):
        counts = {"greenhouse": {M.R.GONE: 1, M.R.MOVED: 1, M.R.MOVED_OFFSITE: 2,
                                 M.R.REVIEW: 9}}
        M.print_summary(("greenhouse",), counts, "x.csv")
        out = capsys.readouterr().out
        assert "4 rows would propose active=0" in out
        assert M.R.MOVED_OFFSITE in out

    def test_a_missing_companies_csv_exits_one(self, M, cli):
        assert M.main(["--audit", str(self._audit(cli)),
                       "--companies", str(cli / "nope.csv")]) == 1

    def test_the_companies_override_is_used(self, M, monkeypatch, cli):
        wide = cli / "wide.csv"
        wide.write_text("ats,name,slug,url\n"
                        "bamboohr,Acme Labs,acmelabs,\n", encoding="utf-8")
        stub_probes(M, monkeypatch, {})
        out = cli / "out.csv"
        assert M.main(["--audit", str(self._audit(cli)), "--no-web",
                       "--companies", str(wide), "--out", str(out)]) == 0
        by_slug = {r["slug"]: r for r in _read(out)}
        assert by_slug["acme"]["verdict"] == M.R.MOVED_OFFSITE
        assert by_slug["acme"]["new_ats"] == "bamboohr"


class TestCompaniesPath:
    def test_the_wide_dump_is_the_default(self, M, monkeypatch, tmp_path):
        wide, narrow = tmp_path / "main.csv", tmp_path / "narrow.csv"
        wide.write_text("ats,name,slug,url\n", encoding="utf-8")
        narrow.write_text("ats,name,slug,url\n", encoding="utf-8")
        monkeypatch.setattr(M, "MAIN_COMPANIES_CSV", wide)
        monkeypatch.setattr(M, "COMPANIES_CSV", narrow)
        assert M.companies_path() == wide

    def test_it_falls_back_when_the_wide_dump_is_absent(self, M, monkeypatch, tmp_path):
        narrow = tmp_path / "narrow.csv"
        narrow.write_text("ats,name,slug,url\n", encoding="utf-8")
        monkeypatch.setattr(M, "MAIN_COMPANIES_CSV", tmp_path / "missing.csv")
        monkeypatch.setattr(M, "COMPANIES_CSV", narrow)
        assert M.companies_path() == narrow

    def test_an_override_wins(self, M, tmp_path):
        assert M.companies_path(str(tmp_path / "x.csv")) == tmp_path / "x.csv"

    def test_a_relative_override_is_repo_relative(self, M):
        from src import paths

        assert M.companies_path("x.csv") == paths.REPO_ROOT / "x.csv"


# --- DNS as the only licence for `gone` -------------------------------------

# The audit rows that read `gone` on a live sample and should not have. Each
# entry is the audit slug, the audit name, the real domain, and what that
# domain's homepage actually does to us.
CORPUS = [
    ("350org", "350org", "350.org", 302),
    ("21co", "21co", "21.co", 0),
    ("2seventybioinc", "2seventybioinc", "2seventybio.com", 301),
    ("abtasty", "abtasty", "abtasty.com", 403),
    ("777partners", "777partners", "777partners.com", 0),
    ("1848ventures", "1848ventures", "1848ventures.com", 404),
    ("54collective", "54collective", "54collective.com", 301),
    ("10up-2", "10up-2", "10up.com", 200),
]


class TestDnsLicensesGone:
    @pytest.mark.parametrize("slug,name,domain,http", CORPUS)
    def test_a_resolving_domain_is_never_gone(self, M, monkeypatch, slug, name,
                                              domain, http):
        """No pages at all: every fetch fails, only DNS answers. That is the
        403/timeout/404 case, and it must not retire the company."""
        stub_probes(M, monkeypatch, {})
        ctx = ctx_for(M, dns={domain: M.R.DNS_OK})
        result = M.resolve_row(row("greenhouse", slug, name), ctx)
        verdict = field(M, result, "verdict")
        assert verdict == M.R.ALIVE_NO_BOARD, verdict
        assert not M.R.is_retireable(verdict)
        assert field(M, result, "domain") == domain
        assert "dns_only" in field(M, result, "evidence")

    @pytest.mark.parametrize("slug,name,domain,http", CORPUS)
    def test_the_candidate_list_reaches_the_real_domain(self, M, monkeypatch,
                                                        slug, name, domain, http):
        stub_probes(M, monkeypatch, {})
        web = stub_web(M, {}, {domain: M.R.DNS_OK})
        M.resolve_row(row("greenhouse", slug, name),
                      M.Context(companies={}, pacer=None, web_factory=web))
        assert domain in web.looked_up

    def test_dns_runs_before_any_http_request(self, M, monkeypatch):
        """The cheap ordering win: a wider TLD list is only affordable because
        the non-existent candidates never cost a fetch."""
        stub_probes(M, monkeypatch, {})
        web = stub_web(M, {"https://acmelabs.com": "<html>x</html>"})
        M.resolve_row(row("lever", "acme", "Acme Labs"),
                      M.Context(companies={}, pacer=None, web_factory=web))
        assert len(web.looked_up) > 1
        assert all(u.startswith("https://acmelabs.com") for u in web.fetched)

    def test_a_dns_failure_that_is_not_nxdomain_poisons_gone(self, M, monkeypatch):
        """A resolver timeout is not an answer. Re-probe, never retire."""
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(
            row("lever", "acme", "Acme Labs"),
            ctx_for(M, dns={"acmelabs.com": M.R.DNS_UNKNOWN}))
        assert field(M, result, "verdict") == M.R.ERROR
        assert "dns_unknown:acmelabs.com" in field(M, result, "evidence")
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_a_resolving_domain_outranks_an_unknown_lookup(self, M, monkeypatch):
        """Existence once established is not undone by a later hiccup."""
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(
            row("lever", "acme", "Acme Labs"),
            ctx_for(M, dns={"acmelabs.com": M.R.DNS_OK,
                            "acmelabs.io": M.R.DNS_UNKNOWN}))
        assert field(M, result, "verdict") == M.R.ALIVE_NO_BOARD

    def test_nxdomain_everywhere_is_still_gone(self, M, monkeypatch):
        """The one input that may license a retirement."""
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("lever", "acme", "Acme Labs"), ctx_for(M))
        assert field(M, result, "verdict") == M.R.GONE
        assert "no_domain" in field(M, result, "evidence")

    def test_a_readable_site_is_high_confidence_a_resolving_one_is_low(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        read = M.resolve_row(row("lever", "acme", "Acme Labs"),
                             ctx_for(M, pages={"https://acmelabs.com": "<p>hi</p>"}))
        assert field(M, read, "confidence") == M.R.HIGH
        assert "site_up_no_board" in field(M, read, "evidence")
        dns_only = M.resolve_row(row("lever", "acme", "Acme Labs"),
                                 ctx_for(M, dns={"acmelabs.com": M.R.DNS_OK}))
        assert field(M, dns_only, "confidence") == M.R.LOW

    def test_a_404_homepage_still_gets_the_careers_paths(self, M, monkeypatch):
        """`1848ventures.com` 404s at the root and still has a careers page."""
        pages = {"https://acmelabs.com/careers": "jobs.ashbyhq.com/acmelabs"}
        stub_probes(M, monkeypatch, {("ashby", "acmelabs"): M.Probe(jobs=3)})
        result = M.resolve_row(
            row("lever", "acme", "Acme Labs"),
            ctx_for(M, pages=pages, dns={"acmelabs.com": M.R.DNS_OK}))
        assert field(M, result, "verdict") == M.R.MOVED
        assert field(M, result, "new_slug") == "acmelabs"

    def test_a_second_resolving_domain_is_tried_when_the_first_is_silent(self, M, monkeypatch):
        pages = {"https://acmelabs.io": "jobs.ashbyhq.com/acmelabs"}
        stub_probes(M, monkeypatch, {("ashby", "acmelabs"): M.Probe(jobs=3)})
        result = M.resolve_row(
            row("lever", "acme", "Acme Labs"),
            ctx_for(M, pages=pages, dns={"acmelabs.com": M.R.DNS_OK,
                                         "acmelabs.io": M.R.DNS_OK}))
        assert field(M, result, "domain") == "acmelabs.io"
        assert field(M, result, "verdict") == M.R.MOVED


class TestWebClientHttp:
    def _client(self, M, monkeypatch, answers):
        """`answers` is a list of (status, text) served in order."""
        seen = []

        class Resp:
            def __init__(self, status, text, url):
                self.status_code, self.text, self.url = status, text, url

        def fake_get(self, url, timeout=None, allow_redirects=None, headers=None):
            seen.append(((headers or {}).get("User-Agent"), allow_redirects))
            status, text = answers[len(seen) - 1]
            return Resp(status, text, url)

        monkeypatch.setattr(M.requests.Session, "get", fake_get)
        return M.WebClient(), seen

    def test_redirects_are_followed(self, M, monkeypatch):
        web, seen = self._client(M, monkeypatch, [(200, "<p>ok</p>")])
        assert web.get("https://acme.com")
        assert seen[0][1] is True

    @pytest.mark.parametrize("status", (403, 406, 429))
    def test_a_blocked_homepage_is_retried_with_a_second_ua(self, M, monkeypatch, status):
        web, seen = self._client(M, monkeypatch, [(status, ""), (200, "<p>ok</p>")])
        got = web.get("https://abtasty.com")
        assert got and got[1] == "<p>ok</p>"
        assert len(seen) == 2
        assert seen[0][0] is None and seen[1][0] == M.ALT_USER_AGENT

    def test_the_retry_happens_once(self, M, monkeypatch):
        web, seen = self._client(M, monkeypatch, [(403, ""), (403, "")])
        assert web.get("https://abtasty.com") is None
        assert len(seen) == 2

    def test_a_404_is_not_retried(self, M, monkeypatch):
        web, seen = self._client(M, monkeypatch, [(404, "<p>nope</p>")])
        assert web.get("https://acme.com") is None
        assert len(seen) == 1

    def test_both_attempts_count_against_the_fetch_budget(self, M, monkeypatch):
        web, seen = self._client(M, monkeypatch, [(403, ""), (200, "<p>ok</p>")])
        web.limit = 1
        assert web.get("https://acme.com") is None
        assert len(seen) == 1

    def test_the_ua_retry_statuses(self, M):
        assert M.UA_RETRY_STATUSES == (403, 406, 429)


class TestDnsClassification:
    def _client(self, M, monkeypatch, err):
        def fake(*a, **k):
            if err is None:
                return [("fam", "type", 0, "", ("1.2.3.4", 0))]
            raise err

        monkeypatch.setattr(M.socket, "getaddrinfo", fake)
        return M.WebClient()

    def test_an_address_is_ok(self, M, monkeypatch):
        assert self._client(M, monkeypatch, None).resolves("acme.com") == M.R.DNS_OK

    def test_no_address_defers_to_the_zone_check(self, M, monkeypatch):
        """EAI_NONAME conflates absence with a name that has no A record, so
        it is never read as absence on its own."""
        import socket as s

        web = self._client(M, monkeypatch, s.gaierror(s.EAI_NONAME, "not known"))
        monkeypatch.setattr(type(web), "zone_state",
                            lambda self, d: M.R.DNS_NXDOMAIN)
        assert web.resolves("acme.com") == M.R.DNS_NXDOMAIN

    def test_a_resolver_timeout_is_unknown_not_absence(self, M, monkeypatch):
        import socket as s

        web = self._client(M, monkeypatch, s.gaierror(s.EAI_AGAIN, "try again"))
        assert web.resolves("acme.com") == M.R.DNS_UNKNOWN

    def test_any_other_exception_is_unknown(self, M, monkeypatch):
        web = self._client(M, monkeypatch, OSError("no network"))
        assert web.resolves("acme.com") == M.R.DNS_UNKNOWN


@pytest.mark.skipif(not os.environ.get("SLUG_LIVE_TESTS"),
                    reason="set SLUG_LIVE_TESTS=1 to re-verify the corpus live")
def test_the_corpus_domains_really_do_resolve():
    """The offline corpus above is only as good as its hand-check. This is the
    re-check: `SLUG_LIVE_TESTS=1 uv run pytest tests/test_reconcile_slugs.py -k really`.
    """
    M = _load()
    web = M.WebClient(limit=0)
    missing = [d for _, _, d, _ in CORPUS if web.resolves(d) != M.R.DNS_OK]
    assert not missing, f"no longer resolving: {missing}"


class TestZoneState:
    """The fallback that keeps `21.co` -- registered, delegated, no A record --
    out of the retirement list. macOS reports it as EAI_NONAME."""

    def _client(self, M, monkeypatch, answers, servers=("10.0.0.1",)):
        sent = []

        class FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, t):
                pass

            def sendto(self, packet, addr):
                sent.append((packet, addr))

            def recv(self, n):
                answer = answers[len(sent) - 1]
                if isinstance(answer, Exception):
                    raise answer
                query_id = sent[-1][0][0] << 8 | sent[-1][0][1]
                return bytes([query_id >> 8, query_id & 0xFF, 0x81,
                              0x80 | answer]) + bytes(8)

        monkeypatch.setattr(M, "NAMESERVERS", servers)
        monkeypatch.setattr(M.socket, "socket", lambda *a, **k: FakeSock())
        return M.WebClient(limit=0), sent

    def test_noerror_means_the_name_exists(self, M, monkeypatch):
        web, _ = self._client(M, monkeypatch, [0])
        assert web.zone_state("21.co") == M.R.DNS_OK

    def test_nxdomain_is_absence(self, M, monkeypatch):
        web, _ = self._client(M, monkeypatch, [3])
        assert web.zone_state("acmelabszzz.io") == M.R.DNS_NXDOMAIN

    def test_a_resolver_that_will_not_answer_leaves_it_unknown(self, M, monkeypatch):
        web, _ = self._client(M, monkeypatch, [TimeoutError("slow")])
        assert web.zone_state("21.co") == M.R.DNS_UNKNOWN

    def test_it_falls_through_to_the_next_resolver(self, M, monkeypatch):
        web, sent = self._client(M, monkeypatch, [TimeoutError("slow"), 0],
                                 servers=("10.0.0.1", "1.1.1.1"))
        assert web.zone_state("21.co") == M.R.DNS_OK
        assert [addr[0] for _, addr in sent] == ["10.0.0.1", "1.1.1.1"]

    def test_an_address_less_name_falls_through_from_getaddrinfo(self, M, monkeypatch):
        import socket as s

        web, _ = self._client(M, monkeypatch, [0])
        monkeypatch.setattr(
            M.socket, "getaddrinfo",
            lambda *a, **k: (_ for _ in ()).throw(s.gaierror(s.EAI_NONAME, "x")))
        assert web.resolves("21.co") == M.R.DNS_OK

    def test_the_system_resolver_is_asked_first(self, M):
        """A local cache makes the junk candidates nearly free."""
        assert M.DNS_FALLBACK_SERVERS == ("1.1.1.1", "8.8.8.8")
        assert M.NAMESERVERS[-2:] == M.DNS_FALLBACK_SERVERS


class TestNoCandidates:
    """The other vacuous route to `gone`: a stem too short to guess from. The
    audit's `21` and `3q` were retired on an empty candidate list."""

    @pytest.mark.parametrize("slug,name", [("21", "21"), ("3q", "3q"),
                                           ("0g", "0g")])
    def test_a_row_with_no_guessable_domain_is_review_not_gone(self, M, monkeypatch,
                                                               slug, name):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("greenhouse", slug, name), ctx_for(M))
        assert field(M, result, "verdict") == M.R.REVIEW
        assert "no_candidates" in field(M, result, "evidence")
        assert not M.R.is_retireable(field(M, result, "verdict"))

    def test_no_lookup_is_even_attempted(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        web = stub_web(M, {})
        M.resolve_row(row("greenhouse", "21", "21"),
                      M.Context(companies={}, pacer=None, web_factory=web))
        assert web.looked_up == [] and web.fetched == []


# --- intra-lane parallelism -------------------------------------------------
# The rows of one lane run concurrently; the shared per-ATS `Pacer` is what
# gates board requests, so widening `--workers` may not raise the request rate
# against any board API. That property is the whole safety argument, so it is
# tested against the real `Pacer` and the real `probe_board`, with only the
# HTTP layer stubbed.

class _CountingWriter:
    """A `Writer` stand-in that only counts, for the pacing measurements."""

    def __init__(self):
        self.rows = []
        self._lock = threading.Lock()

    def write(self, row):
        with self._lock:
            self.rows.append(row)


def _paced_probe_times(M, monkeypatch, rows, workers, pacing):
    """Board-probe timestamps from one lane run at `workers`, real pacer."""
    stamps = []
    lock = M.threading.Lock()

    def fake_fetch_json(url, *a, **kw):
        with lock:
            stamps.append(M.time.monotonic())
        raise CareersError("no board", status=404)

    monkeypatch.setattr(M, "fetch_json", fake_fetch_json)
    pacer = M.Pacer({ats: pacing for ats in M.R.SUPPORTED_ATS})
    ctx = M.Context(companies={}, pacer=pacer, no_web=True)
    counts = {}
    started = M.time.monotonic()
    M.reconcile_lane(rows, ctx, _CountingWriter(), counts, workers)
    return stamps, M.time.monotonic() - started, counts


def _lever_rows(n):
    """Lever/dead rows, so tier 1 fires a casing retry per row and every row
    therefore costs several real board probes."""
    return [row("lever", f"acme{i}", f"Acme {i}", "dead") for i in range(n)]


class TestWorkersDoNotRaiseTheAtsRequestRate:
    PACING = 0.05

    def _min_gap(self, stamps):
        return min((b - a for a, b in zip(stamps, stamps[1:])), default=None)

    def test_the_pacing_floor_holds_at_one_worker(self, M, monkeypatch):
        stamps, _, _ = _paced_probe_times(M, monkeypatch, _lever_rows(3), 1,
                                          self.PACING)
        assert len(stamps) > 3
        assert self._min_gap(stamps) >= self.PACING

    @pytest.mark.parametrize("workers", [2, 8, 32])
    def test_the_same_floor_holds_however_wide_the_lane_is(self, M, monkeypatch,
                                                           workers):
        stamps, _, _ = _paced_probe_times(M, monkeypatch, _lever_rows(6),
                                          workers, self.PACING)
        assert len(stamps) > 6
        assert self._min_gap(stamps) >= self.PACING

    def test_the_request_rate_does_not_rise_with_workers(self, M, monkeypatch):
        """Same rows, 16x the threads: the same number of probes, and no more
        than the pacer's rate of them."""
        rows = _lever_rows(8)
        serial, elapsed_1, _ = _paced_probe_times(M, monkeypatch, rows, 1,
                                                  self.PACING)
        wide, elapsed_n, _ = _paced_probe_times(M, monkeypatch, rows, 16,
                                                self.PACING)
        assert len(wide) == len(serial)
        for stamps, elapsed in ((serial, elapsed_1), (wide, elapsed_n)):
            assert len(stamps) / max(elapsed, 1e-9) <= 1.0 / self.PACING + 1

    def test_every_row_is_still_answered(self, M, monkeypatch):
        _, _, counts = _paced_probe_times(M, monkeypatch, _lever_rows(9), 8,
                                          self.PACING)
        assert sum(counts.values()) == 9

    def test_the_pacer_is_the_only_gate(self, M, monkeypatch):
        """An unpaced lane is unpaced at any width -- the thread count is not a
        second, independent throttle."""
        stamps, _, _ = _paced_probe_times(M, monkeypatch, _lever_rows(4), 8, 0)
        assert len(stamps) > 4


class TestParallelLaneBookkeeping:
    def test_every_row_is_written_exactly_once(self, M, monkeypatch, tmp_path):
        stub_probes(M, monkeypatch, {})
        out = tmp_path / "r.csv"
        writer = M.Writer(out, existing=False)
        rows = [row("greenhouse", f"acme{i}", f"Acme Labs {i}") for i in range(40)]
        counts = {}
        try:
            M.reconcile_lane(rows, ctx_for(M), writer, counts, workers=8)
        finally:
            writer.close()
        written = _read(out)
        assert len(written) == 40
        assert {r["slug"] for r in written} == {f"acme{i}" for i in range(40)}
        assert sum(counts.values()) == 40

    def test_the_counts_survive_concurrent_increments(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        counts = {}
        rows = [row("greenhouse", f"acme{i}", f"Acme Labs {i}") for i in range(60)]
        M.reconcile_lane(rows, ctx_for(M), _CountingWriter(), counts, workers=16)
        assert sum(counts.values()) == 60

    def test_the_writer_holds_its_lock_across_the_flush(self, M, tmp_path):
        """Many threads, one file: no interleaved or partial CSV lines."""
        out = tmp_path / "r.csv"
        writer = M.Writer(out, existing=False)
        blank = [""] * (len(M.R.COLUMNS) - 2)

        def write(i):
            for j in range(20):
                writer.write(["greenhouse", f"s{i}-{j}"] + blank)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        writer.close()
        written = _read(out)
        assert len(written) == 240
        assert len({r["slug"] for r in written}) == 240
        assert all(len(r) == len(M.R.COLUMNS) for r in written)

    def test_one_worker_is_still_a_plain_loop(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        counts = {}
        writer = _CountingWriter()
        rows = [row("greenhouse", f"acme{i}", f"Acme Labs {i}") for i in range(4)]
        M.reconcile_lane(rows, ctx_for(M), writer, counts, workers=1)
        assert [r[1] for r in writer.rows] == [f"acme{i}" for i in range(4)]


class TestWorkersFlag:
    def test_the_default_is_eight_per_lane(self, M):
        assert M.DEFAULT_WORKERS == 8
        assert M.parse_args(["--audit", "a.csv"]).workers == 8

    def test_it_is_settable(self, M):
        assert M.parse_args(["--audit", "a.csv", "--workers", "24"]).workers == 24

    def test_main_passes_it_down(self, M, monkeypatch, cli):
        stub_probes(M, monkeypatch, {})
        seen = []
        original = M.reconcile_lane

        def spy(rows, ctx, writer, counts, workers=M.DEFAULT_WORKERS):
            seen.append(workers)
            return original(rows, ctx, writer, counts, workers)

        monkeypatch.setattr(M, "reconcile_lane", spy)
        audit = cli / "audit.csv"
        audit.write_text(
            "ats,slug,name,status,postings,newest_posting,http,note\n"
            "greenhouse,acme,Acme Labs,dead,0,,404,\n", encoding="utf-8")
        out = cli / "out.csv"
        assert M.main(["--audit", str(audit), "--no-web", "--workers", "5",
                       "--out", str(out)]) == 0
        assert seen == [5]

    @pytest.mark.parametrize("given", ["0", "-4"])
    def test_a_nonsense_width_floors_at_one(self, M, monkeypatch, cli, given):
        stub_probes(M, monkeypatch, {})
        seen = []
        monkeypatch.setattr(M, "reconcile_lane",
                            lambda *a: seen.append(a[-1]))
        audit = cli / "audit.csv"
        audit.write_text(
            "ats,slug,name,status,postings,newest_posting,http,note\n"
            "greenhouse,acme,Acme Labs,dead,0,,404,\n", encoding="utf-8")
        assert M.main(["--audit", str(audit), "--no-web", "--workers", given,
                       "--out", str(cli / "out.csv")]) == 0
        assert seen == [1]


class TestResumeUnderParallelism:
    def test_a_parallel_run_resumes_without_repeating_a_row(self, M, monkeypatch,
                                                            cli):
        """The done set is read before any worker starts and filters the
        worklist, so widening the lane cannot reprocess a written row."""
        stub_probes(M, monkeypatch, {})
        audit = cli / "audit.csv"
        audit.write_text(
            "ats,slug,name,status,postings,newest_posting,http,note\n"
            + "".join(f"greenhouse,acme{i},Acme Labs {i},dead,0,,404,\n"
                      for i in range(12)),
            encoding="utf-8")
        out = cli / "out.csv"
        args = ["--audit", str(audit), "--no-web", "--workers", "6",
                "--out", str(out)]
        assert M.main(args + ["--limit", "5"]) == 0
        first = {r["slug"] for r in _read(out)}
        assert len(first) == 5

        assert M.main(args + ["--resume"]) == 0
        slugs = [r["slug"] for r in _read(out)]
        assert len(slugs) == len(set(slugs)) == 12
        assert first <= set(slugs)


class TestTruncationEvidence:
    def test_a_truncated_slug_is_flagged_in_the_evidence(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        slug = "roundpointmortgageservicingcor"
        pages = {"https://roundpointmortgage.com": "<html>hi</html>"}
        result = M.resolve_row(row("greenhouse", slug, slug),
                              ctx_for(M, pages=pages))
        assert "truncated_slug" in field(M, result, "evidence")
        assert field(M, result, "domain") == "roundpointmortgage.com"
        assert field(M, result, "verdict") == M.R.ALIVE_NO_BOARD

    def test_a_normal_slug_is_not_flagged(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("greenhouse", "acmelabs", "Acme Labs"),
                              ctx_for(M))
        assert "truncated_slug" not in field(M, result, "evidence")

    def test_no_candidates_means_no_truncation_flag_either(self, M, monkeypatch):
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("greenhouse", "0g", "0g"), ctx_for(M))
        assert "truncated_slug" not in field(M, result, "evidence")


class TestMangledSlugsAreNotGone:
    """The live sample's false positives, end to end through the cascade: the
    board 404s, nothing matches by name, and the only thing standing between
    the row and `gone` is a candidate domain that resolves."""

    @pytest.mark.parametrize("slug,domain", [
        ("pjtpartnersprofessionals", "pjtpartners.com"),
        ("evolutionparkingguestservices", "evolutionparking.com"),
        ("roundpointmortgageservicingcor", "roundpointmortgage.com"),
        ("halodotscience", "halo.science"),
        ("centerforhumancompatibleartifi", "humancompatible.ai"),
        ("shrapnelstudio", "shrapnel.com"),
        ("zscalerredcanary", "zscaler.com"),
    ])
    def test_a_resolving_candidate_blocks_the_retirement(self, M, monkeypatch,
                                                         slug, domain):
        stub_probes(M, monkeypatch, {})
        ctx = ctx_for(M, dns={domain: M.R.DNS_OK})
        result = M.resolve_row(row("greenhouse", slug, slug), ctx)
        assert field(M, result, "verdict") == M.R.ALIVE_NO_BOARD
        assert field(M, result, "domain") == domain
        assert not M.R.retires_old_row(field(M, result, "verdict"))

    def test_the_same_row_with_nothing_resolving_is_still_gone(self, M, monkeypatch):
        """The wider candidate list must not have weakened the gate."""
        stub_probes(M, monkeypatch, {})
        result = M.resolve_row(row("greenhouse", "shrapnelstudio", "shrapnelstudio"),
                               ctx_for(M))
        assert field(M, result, "verdict") == M.R.GONE


class TestWebTimeout:
    def test_the_row_budget_gets_the_tighter_timeout(self, M, monkeypatch):
        """Up to MAX_WEB_FETCHES fetches a row; the timeout is the ceiling on
        what one unresponsive host can cost."""
        assert M.WEB_TIMEOUT == 6
        seen = {}

        class FakeResponse:
            status_code, text, url = 200, "<html></html>", "https://acme.com"

        class FakeSession:
            headers = {}

            def update(self, *a):
                pass

            def get(self, url, **kw):
                seen.update(kw)
                return FakeResponse()

        session = FakeSession()
        session.headers = type("H", (), {"update": lambda *a: None})()
        monkeypatch.setattr(M.requests, "Session", lambda: session)
        web = M.WebClient()
        web.get("https://acme.com")
        assert seen["timeout"] == 6


class TestDnsSweepStopsOnceExistenceIsSettled:
    """The sweep is wide now, so it stops at `MAX_DOMAIN_SITES` resolving
    names -- the most that are ever read. It can only stop early when the row
    is already un-retireable, so the `gone` gate is untouched."""

    def test_it_stops_at_the_sites_it_can_read(self, M):
        looked_up = []

        class AllResolve:
            def resolves(self, domain):
                looked_up.append(domain)
                return M.R.DNS_OK

        candidates = M.R.domain_candidates("Acme Labs", "acmelabs")
        out = M.R.Outcome(old_status="dead")
        resolving = M._resolve_domains(candidates, AllResolve(), out, M.RowBudget())
        assert len(resolving) == len(looked_up) == M.MAX_DOMAIN_SITES
        assert not out.errors

    def test_nothing_resolving_still_sweeps_the_whole_list(self, M, monkeypatch):
        """`gone` needs the empty list, so that path may never short-circuit."""
        stub_probes(M, monkeypatch, {})
        web = stub_web(M, {})
        client = web()
        candidates = M.R.domain_candidates("Acme Labs", "acmelabs")
        out = M.R.Outcome(old_status="dead")
        assert M._resolve_domains(candidates, client, out, M.RowBudget()) == []
        assert len(web.looked_up) == min(len(candidates), M.MAX_DNS_LOOKUPS)

    def test_the_dns_cap_tracks_the_candidate_cap(self, M):
        assert M.MAX_DNS_LOOKUPS == M.R.MAX_DOMAIN_CANDIDATES == 220
