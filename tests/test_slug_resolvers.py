"""scripts/slug_resolvers.py: the pure half of the reconcile pass.

The safety-critical claim is the verdict table -- only `moved` (on the old row)
and `gone` may ever propose `active=0`, and a 404 alone is never enough for
either. Everything else here is regex and normalization, which is exactly the
part that silently mis-fires: the greenhouse embed form that captures "embed"
instead of the `for=` token, a generic one-word name that collides across four
unrelated companies, a Lever slug whose only fault is its casing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "slug_resolvers.py"


def _load():
    spec = importlib.util.spec_from_file_location("slug_resolvers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the dataclasses below resolve their annotations
    # through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def R():
    return _load()


# --- the retirement predicate ----------------------------------------------

class TestRetireable:
    def test_only_moved_and_gone(self, R):
        assert R.RETIREABLE == {R.MOVED, R.GONE}

    def test_no_other_verdict_is_retireable(self, R):
        for verdict in R.VERDICTS:
            expected = verdict in (R.MOVED, R.GONE)
            assert R.is_retireable(verdict) is expected, verdict

    def test_alive_no_board_and_error_never_retire(self, R):
        assert not R.is_retireable(R.ALIVE_NO_BOARD)
        assert not R.is_retireable(R.ERROR)
        assert not R.is_retireable(R.DUAL)
        assert not R.is_retireable(R.REVIEW)
        assert not R.is_retireable(R.OK)

    def test_an_unknown_string_is_not_retireable(self, R):
        assert not R.is_retireable("")
        assert not R.is_retireable("dead")

    def test_moved_offsite_retires_the_old_row_without_an_addition(self, R):
        """It proposes active=0 but no new board: the destination is a platform
        we have no client for, so adding it would create an unpollable slug."""
        assert R.MOVED_OFFSITE in R.RETIREABLE_OFFSITE
        assert R.MOVED_OFFSITE not in R.RETIREABLE
        assert R.retires_old_row(R.MOVED_OFFSITE)
        assert not R.is_retireable(R.MOVED_OFFSITE)

    def test_the_two_retirement_routes_are_disjoint(self, R):
        assert not (R.RETIREABLE & R.RETIREABLE_OFFSITE)
        assert R.RETIRES_OLD_ROW == R.RETIREABLE | R.RETIREABLE_OFFSITE

    def test_no_other_verdict_proposes_active_zero(self, R):
        for verdict in R.VERDICTS:
            expected = verdict in (R.MOVED, R.MOVED_OFFSITE, R.GONE)
            assert R.retires_old_row(verdict) is expected, verdict


class TestSupportedAts:
    @pytest.mark.parametrize("ats", ["greenhouse", "lever", "ashby", "workday"])
    def test_the_four_lanes_we_scrape(self, R, ats):
        assert R.is_supported_ats(ats)

    @pytest.mark.parametrize("ats", ["bamboohr", "workable", "smartrecruiters",
                                     "rippling", "gem", "personio", "join_com", ""])
    def test_everything_else_is_unsupported(self, R, ats):
        assert not R.is_supported_ats(ats)


# --- the verdict decision ---------------------------------------------------

def _out(R, status, **kw):
    out = R.Outcome(old_status=status)
    out.input_live = status in R.LIVE_STATUSES
    for k, v in kw.items():
        setattr(out, k, v)
    return out


def _cand(R, confidence="high"):
    return R.Candidate("workday", "learfield|wd5|Paciolan", 12, "tier2_name", confidence)


class TestDecideVerdict:
    def test_live_board_with_no_migration_is_ok(self, R):
        assert R.decide_verdict(_out(R, "active"))[0] == R.OK

    def test_a_stale_board_with_no_migration_is_still_ok(self, R):
        """Stale means live-with-old-postings; it is not a retirement signal."""
        assert R.decide_verdict(_out(R, "stale"))[0] == R.OK

    def test_a_high_confidence_find_on_a_dead_row_is_moved(self, R):
        verdict, conf = R.decide_verdict(_out(R, "dead", found=_cand(R)))
        assert (verdict, conf) == (R.MOVED, R.HIGH)
        assert R.is_retireable(verdict)

    def test_a_low_confidence_find_is_review_never_moved(self, R):
        verdict, conf = R.decide_verdict(_out(R, "dead", found=_cand(R, "low")))
        assert verdict == R.REVIEW
        assert conf == R.LOW
        assert not R.is_retireable(verdict)

    def test_a_find_on_a_live_board_is_dual(self, R):
        verdict, _ = R.decide_verdict(_out(R, "active", found=_cand(R)))
        assert verdict == R.DUAL
        assert not R.is_retireable(verdict)

    def test_dual_wins_even_when_the_find_is_low_confidence(self, R):
        verdict, conf = R.decide_verdict(_out(R, "active", found=_cand(R, "low")))
        assert (verdict, conf) == (R.DUAL, R.LOW)

    def test_a_dead_board_with_a_live_website_is_alive_no_board(self, R):
        verdict, _ = R.decide_verdict(_out(R, "dead", domain="acme.com"))
        assert verdict == R.ALIVE_NO_BOARD
        assert not R.is_retireable(verdict)

    def test_gone_needs_a_404_and_nothing_else_found(self, R):
        verdict, _ = R.decide_verdict(_out(R, "dead", web_checked=True))
        assert verdict == R.GONE
        assert R.is_retireable(verdict)

    def test_a_skipped_website_check_is_never_gone(self, R):
        """`--no-web` leaves tier 3 unrun. Not looking is not evidence."""
        verdict, _ = R.decide_verdict(_out(R, "dead"))
        assert verdict == R.REVIEW
        assert not R.is_retireable(verdict)

    def test_an_empty_board_is_never_gone(self, R):
        """`empty` is a 200 with no postings -- the board exists. Retiring it on
        the same evidence as a 404 would drop a company between hiring rounds."""
        verdict, _ = R.decide_verdict(_out(R, "empty", web_checked=True))
        assert verdict == R.REVIEW
        assert not R.is_retireable(verdict)

    def test_an_error_outranks_gone(self, R):
        """403/429/5xx/timeout means we learned nothing. Re-probe, never retire."""
        verdict, _ = R.decide_verdict(
            _out(R, "dead", web_checked=True, errors=["lever/acme:403"]))
        assert verdict == R.ERROR
        assert not R.is_retireable(verdict)

    def test_an_error_outranks_alive_no_board(self, R):
        verdict, _ = R.decide_verdict(
            _out(R, "dead", domain="acme.com", errors=["ashby/acme:429"]))
        assert verdict == R.ERROR

    def test_a_confirmed_find_outranks_an_error_on_another_candidate(self, R):
        verdict, _ = R.decide_verdict(
            _out(R, "dead", found=_cand(R), errors=["ashby/acme:500"]))
        assert verdict == R.MOVED

    def test_an_offsite_match_on_a_dead_row_is_moved_offsite(self, R):
        verdict, conf = R.decide_verdict(
            _out(R, "dead", offsite=R.Offsite("bamboohr", "acmelabs")))
        assert (verdict, conf) == (R.MOVED_OFFSITE, R.HIGH)
        assert R.retires_old_row(verdict)
        assert not R.is_retireable(verdict)

    def test_a_low_confidence_offsite_match_is_review(self, R):
        verdict, conf = R.decide_verdict(
            _out(R, "dead", offsite=R.Offsite("bamboohr", "loop", R.LOW)))
        assert (verdict, conf) == (R.REVIEW, R.LOW)
        assert not R.retires_old_row(verdict)

    @pytest.mark.parametrize("status", ["empty", "stale", "active"])
    def test_a_board_that_answered_200_is_never_moved_offsite(self, R, status):
        """Same gate as `moved`: only a confirmed 404 may be retired."""
        verdict, _ = R.decide_verdict(
            _out(R, status, offsite=R.Offsite("bamboohr", "acmelabs")))
        assert verdict != R.MOVED_OFFSITE
        assert not R.retires_old_row(verdict)

    def test_an_error_outranks_moved_offsite(self, R):
        """Nothing was probed here, so an unrelated failure still means the row
        must be re-run rather than retired on an unverified match."""
        verdict, _ = R.decide_verdict(
            _out(R, "dead", offsite=R.Offsite("bamboohr", "acmelabs"),
                 errors=["ashby/acmelabs:429"]))
        assert verdict == R.ERROR
        assert not R.retires_old_row(verdict)

    def test_a_probed_board_outranks_an_offsite_match(self, R):
        verdict, _ = R.decide_verdict(
            _out(R, "dead", found=_cand(R), offsite=R.Offsite("bamboohr", "acmelabs")))
        assert verdict == R.MOVED

    def test_every_status_reaches_a_known_verdict(self, R):
        for status in R.INPUT_STATUSES:
            verdict, conf = R.decide_verdict(_out(R, status))
            assert verdict in R.VERDICTS
            assert conf in (R.HIGH, R.LOW)


# --- name normalization -----------------------------------------------------

class TestNormalizeName:
    @pytest.mark.parametrize("raw,expected", [
        ("Acme Inc.", "acme"),
        ("Acme, Inc.", "acme"),
        ("ACME LLC", "acme"),
        ("Acme Technologies Ltd", "acme technologies"),
        ("Acme Corporation", "acme"),
        ("Acme GmbH", "acme"),
        ("Acme Pte. Ltd.", "acme"),
        ("O'Reilly Media", "oreilly media"),
        ("Acme’s Labs", "acmes labs"),
        ("  Acme   Labs  ", "acme labs"),
        ("Acme & Co", "acme"),
        ("Acme-Beta", "acme beta"),
        ("", ""),
    ])
    def test_cases(self, R, raw, expected):
        assert R.normalize_name(raw) == expected

    def test_a_suffix_word_inside_a_name_is_still_stripped(self, R):
        """`co` is a suffix token wherever it appears; the cost is a few odd
        stems, the benefit is that `Acme Co Labs` and `Acme Labs` match."""
        assert R.normalize_name("Acme Co Labs") == "acme labs"

    def test_stem_removes_separators(self, R):
        assert R.name_stem("Acme Labs, Inc.") == "acmelabs"


class TestAmbiguousName:
    @pytest.mark.parametrize("name", ["loop", "slate", "astra", "copper", "crisp",
                                      "0g", "", "quantum", "vertex"])
    def test_short_or_generic_names_are_ambiguous(self, R, name):
        assert R.is_ambiguous_name(name)

    @pytest.mark.parametrize("name", ["learfield", "digital turbine", "paciolan",
                                      "loop earplugs", "slate auto"])
    def test_distinctive_and_multiword_names_are_not(self, R, name):
        assert not R.is_ambiguous_name(name)

    def test_the_length_boundary(self, R):
        assert R.is_ambiguous_name("a" * R.AMBIGUOUS_MAX_LEN)
        assert not R.is_ambiguous_name("a" * (R.AMBIGUOUS_MAX_LEN + 1))


# --- companies.csv ----------------------------------------------------------

class TestLoadCompanies:
    def test_indexes_by_normalized_name(self, R, tmp_path):
        p = tmp_path / "companies.csv"
        p.write_text(
            "ats,name,slug,url\n"
            "lever,Fliff Inc.,Fliff,https://jobs.lever.co/Fliff\n"
            "workday,Learfield,learfield,https://learfield.wd5.myworkdayjobs.com/Paciolan\n"
            ",Broken,x,\n"
            "ashby,NoSlug,,https://x\n",
            encoding="utf-8")
        index = R.load_companies(p)
        assert [c.slug for c in index["fliff"]] == ["Fliff"]
        assert index["learfield"][0].ats == "workday"
        assert "broken" not in index and "noslug" not in index

    def test_the_real_csv_loads(self, R):
        index = R.load_companies(REPO_ROOT / "companies.csv")
        assert len(index) > 1000
        assert all(isinstance(v, list) for v in index.values())

    def test_unsupported_platforms_are_kept_and_flagged(self, R, tmp_path):
        """A bamboohr row is the whole point of the wide dump: it proves the
        company exists even though we can never poll that board."""
        p = tmp_path / "main_companies.csv"
        p.write_text(
            "ats,name,slug,url\n"
            "bamboohr,Acme Labs,acmelabs,https://acmelabs.bamboohr.com/careers\n"
            "greenhouse,Acme Labs,acme-labs,https://boards.greenhouse.io/acme-labs\n",
            encoding="utf-8")
        rows = R.load_companies(p)["acme labs"]
        assert {c.ats for c in rows} == {"bamboohr", "greenhouse"}
        assert {c.ats: c.supported for c in rows} == {"bamboohr": False,
                                                      "greenhouse": True}

    def test_the_wide_dump_loads_and_covers_more_platforms(self, R):
        wide = REPO_ROOT / "main_companies.csv"
        if not wide.exists():
            pytest.skip("main_companies.csv is not present in this clone")
        index = R.load_companies(wide)
        platforms = {c.ats for rows in index.values() for c in rows}
        assert set(R.SUPPORTED_ATS) <= platforms
        assert len(platforms - set(R.SUPPORTED_ATS)) > 10


# --- a name match anywhere forbids `gone` -----------------------------------

class TestAnyNameMatchForbidsGone:
    """The safety claim of the wide dump: a company that appears on any
    platform exists, so no combination of the other facts may retire it as
    `gone`. Asserted over the whole fact space, not on one happy path.
    """

    def _sweep(self, R, **fixed):
        import itertools
        for status, dom, web, errs in itertools.product(
                R.INPUT_STATUSES + ("error",), ("", "acme.com"),
                (False, True), ((), ("403",))):
            out = R.Outcome(old_status=status, domain=dom, web_checked=web,
                            errors=list(errs), **fixed)
            out.input_live = status in R.LIVE_STATUSES
            yield out

    def test_an_offsite_match_can_never_be_gone(self, R):
        for out in self._sweep(R, name_match=True,
                               offsite=R.Offsite("bamboohr", "acmelabs")):
            assert R.decide_verdict(out)[0] != R.GONE, out

    def test_a_name_match_with_nothing_probed_can_never_be_gone(self, R):
        """The supported-lane match that 404'd on every candidate still proves
        the company was in someone's live board dump."""
        for out in self._sweep(R, name_match=True):
            assert R.decide_verdict(out)[0] != R.GONE, out

    def test_without_a_name_match_gone_is_still_reachable(self, R):
        """The guard must not have quietly disabled the verdict."""
        reached = [R.decide_verdict(o)[0] for o in self._sweep(R)]
        assert R.GONE in reached


# --- workday url parsing ----------------------------------------------------

class TestWorkdaySlugFromUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://learfield.wd5.myworkdayjobs.com/Paciolan", "learfield|wd5|Paciolan"),
        ("https://learfield.wd5.myworkdayjobs.com/en-US/Paciolan", "learfield|wd5|Paciolan"),
        ("https://digitalturbine.wd501.myworkdayjobs.com/en-US/DT_Careers",
         "digitalturbine|wd501|DT_Careers"),
        ("learfield.wd5.myworkdayjobs.com/Paciolan", "learfield|wd5|Paciolan"),
        ("https://acme-corp.wd1.myworkdayjobs.com/External?q=x", "acme-corp|wd1|External"),
    ])
    def test_tri_part_slug(self, R, url, expected):
        assert R.workday_slug_from_url(url) == expected

    @pytest.mark.parametrize("url", ["", "https://jobs.lever.co/acme",
                                     "https://acme.myworkdayjobs.com/External",
                                     "https://acme.wdX.myworkdayjobs.com/External"])
    def test_a_non_workday_url_is_none(self, R, url):
        assert R.workday_slug_from_url(url) is None

    def test_the_parsed_slug_is_what_workday_parse_slug_accepts(self, R):
        from src.discovery.sources.ats import workday as wd

        slug = R.workday_slug_from_url("https://learfield.wd5.myworkdayjobs.com/Paciolan")
        assert wd.parse_slug(slug) == ("learfield", "wd5", "Paciolan")

    def test_tenant(self, R):
        assert R.workday_tenant("learfield|wd5|Paciolan") == "learfield"
        assert R.workday_tenant("") == ""


# --- lever casing -----------------------------------------------------------

class TestLeverCaseVariants:
    def test_the_companies_csv_casing_comes_first(self, R):
        assert R.lever_case_variants("fliff", ["Fliff"])[0] == "Fliff"

    def test_generates_the_common_variants(self, R):
        variants = R.lever_case_variants("fliff")
        assert "Fliff" in variants and "FLIFF" in variants

    def test_the_original_is_excluded(self, R):
        assert "fliff" not in R.lever_case_variants("fliff")
        assert "fliff" not in R.lever_case_variants("fliff", ["fliff"])

    def test_segments_are_title_cased(self, R):
        assert "Acme-Labs" in R.lever_case_variants("acme-labs")
        assert "Acme_Labs" in R.lever_case_variants("acme_labs")

    def test_no_duplicates(self, R):
        variants = R.lever_case_variants("ACME")
        assert len(variants) == len(set(variants))

    def test_an_empty_slug_yields_nothing(self, R):
        assert R.lever_case_variants("") == []


# --- domain candidates ------------------------------------------------------

class TestDomainCandidates:
    def test_the_expected_shapes_are_all_present(self, R):
        got = R.domain_candidates("Acme Labs, Inc.", "acmelabs")
        for d in ("acmelabs.com", "acmelabs.io", "acmelabs.ai", "acmelabs.co",
                  "acmelabs.so", "getacmelabs.com", "acme-labs.com"):
            assert d in got

    def test_the_slug_itself_is_a_candidate(self, R):
        assert "zeta.com" in R.domain_candidates("Some Long Legal Name", "zeta")

    def test_a_workday_slug_contributes_only_its_tenant(self, R):
        got = R.domain_candidates("Learfield", "learfield|wd5|Paciolan")
        assert "learfield.com" in got
        assert not any("|" in d for d in got)

    def test_stems_under_three_characters_are_dropped(self, R):
        assert R.domain_candidates("0g", "0g") == []

    def test_capped_and_deduped(self, R):
        got = R.domain_candidates("Acme", "acme")
        assert len(got) == len(set(got)) <= R.MAX_DOMAIN_CANDIDATES


# --- ATS link extraction ----------------------------------------------------

class TestExtractAtsLinks:
    def _one(self, R, html, ats):
        return [l for l in R.extract_ats_links(html) if l.ats == ats]

    def test_greenhouse_board_url(self, R):
        got = self._one(R, '<a href="https://boards.greenhouse.io/acme">Jobs</a>', "greenhouse")
        assert [l.slug for l in got] == ["acme"]

    def test_greenhouse_new_host(self, R):
        got = self._one(R, 'https://job-boards.greenhouse.io/acmelabs', "greenhouse")
        assert [l.slug for l in got] == ["acmelabs"]

    def test_the_embed_form_captures_the_for_token_not_the_word_embed(self, R):
        """The prototype's regex matched the embed script URL and captured
        nothing useful; the `for=` token is the slug."""
        html = ('<script src="https://boards.greenhouse.io/embed/job_board/js?'
                'for=acmelabs"></script>')
        got = self._one(R, html, "greenhouse")
        assert [l.slug for l in got] == ["acmelabs"]
        assert "embed" not in [l.slug for l in got]

    def test_the_embed_form_survives_an_html_escaped_ampersand(self, R):
        html = ('https://boards.greenhouse.io/embed/job_board?token=1&amp;for=acmelabs')
        assert "acmelabs" in [l.slug for l in self._one(R, html, "greenhouse")]

    def test_embed_never_leaks_as_a_slug(self, R):
        html = 'https://boards.greenhouse.io/embed/job_board?for=acme'
        assert [l.slug for l in self._one(R, html, "greenhouse")] == ["acme"]

    def test_lever_both_regions(self, R):
        html = 'jobs.lever.co/Fliff and jobs.eu.lever.co/acme-gmbh'
        assert {l.slug for l in self._one(R, html, "lever")} == {"Fliff", "acme-gmbh"}

    def test_ashby(self, R):
        assert [l.slug for l in self._one(R, 'https://jobs.ashbyhq.com/0g', "ashby")] == ["0g"]

    def test_workday_yields_a_tri_part_slug(self, R):
        html = '<a href="https://learfield.wd5.myworkdayjobs.com/en-US/Paciolan">Careers</a>'
        got = self._one(R, html, "workday")
        assert [l.slug for l in got] == ["learfield|wd5|Paciolan"]

    def test_supported_lanes_come_before_unsupported_ones(self, R):
        html = 'apply.workable.com/acme and boards.greenhouse.io/acme'
        links = R.extract_ats_links(html)
        assert links[0].ats == "greenhouse" and links[0].supported
        assert links[-1].ats == "workable" and not links[-1].supported

    @pytest.mark.parametrize("html,platform", [
        ("https://apply.workable.com/acme/", "workable"),
        ("https://careers.smartrecruiters.com/AcmeLabs", "smartrecruiters"),
        ("https://ats.rippling.com/acme/jobs", "rippling"),
        ("https://jobs.jobvite.com/acme", "jobvite"),
        ("https://acme.bamboohr.com/careers", "bamboohr"),
        ("https://careers-acme.icims.com/jobs", "icims"),
        ("https://recruiting.paylocity.com/recruiting/jobs/All/abc", "paylocity"),
        ("https://acme.teamtailor.com/jobs", "teamtailor"),
        ("https://acme.recruitee.com/", "recruitee"),
        ("https://acme.jobs.personio.com/", "personio"),
    ])
    def test_the_platforms_we_do_not_scrape_are_recorded(self, R, html, platform):
        links = [l for l in R.extract_ats_links(html) if l.ats == platform]
        assert links and not links[0].supported

    def test_unsupported_markers(self, R):
        links = R.extract_ats_links(
            "apply.workable.com/acme boards.greenhouse.io/acme")
        markers = R.unsupported_markers(links)
        assert markers == ["unsupported_ats:workable=acme"]

    def test_deduped_on_ats_and_slug(self, R):
        html = "boards.greenhouse.io/acme " * 5
        assert len(R.extract_ats_links(html)) == 1

    def test_asset_paths_are_not_slugs(self, R):
        assert R.extract_ats_links("https://boards.greenhouse.io/assets/app.js") == []

    def test_no_ats_link_is_an_empty_list(self, R):
        assert R.extract_ats_links("<html><body>hello</body></html>") == []
        assert R.extract_ats_links("") == []
        assert R.extract_ats_links(None) == []


# --- the careers-page hop ---------------------------------------------------

class TestCareersLinks:
    def test_finds_a_link_by_href(self, R):
        html = '<a href="/company/work-with-us/careers">Team</a>'
        assert R.careers_links(html, "https://acme.com/") == [
            "https://acme.com/company/work-with-us/careers"]

    def test_finds_a_link_by_anchor_text(self, R):
        html = '<a href="/x7">Open <b>Jobs</b></a>'
        assert R.careers_links(html, "https://acme.com/") == ["https://acme.com/x7"]

    def test_relative_hrefs_are_resolved_against_the_final_url(self, R):
        html = '<a href="careers">Careers</a>'
        assert R.careers_links(html, "https://www.acme.com/en/") == [
            "https://www.acme.com/en/careers"]

    def test_an_off_host_careers_subdomain_is_followed(self, R):
        html = '<a href="https://careers.acme.com/">Careers</a>'
        assert R.careers_links(html, "https://acme.com/") == ["https://careers.acme.com/"]

    def test_non_http_schemes_and_anchors_are_skipped(self, R):
        html = ('<a href="#jobs">Jobs</a><a href="mailto:jobs@acme.com">Jobs</a>'
                '<a href="javascript:void(0)">Careers</a>')
        assert R.careers_links(html, "https://acme.com/") == []

    def test_unrelated_links_are_skipped(self, R):
        assert R.careers_links('<a href="/about">About</a>', "https://acme.com/") == []

    def test_deduped_and_capped(self, R):
        html = "".join(f'<a href="/careers{i}">Careers</a>' for i in range(20))
        assert len(R.careers_links(html, "https://acme.com/")) == R.MAX_CAREER_HOPS
        html = '<a href="/careers">Careers</a><a href="/careers">Jobs</a>'
        assert R.careers_links(html, "https://acme.com/") == ["https://acme.com/careers"]

    def test_html_entities_in_the_href_are_unescaped(self, R):
        html = '<a href="/careers?a=1&amp;b=2">Careers</a>'
        assert R.careers_links(html, "https://acme.com/") == [
            "https://acme.com/careers?a=1&b=2"]


# --- acquisitions -----------------------------------------------------------

class TestAcquirerOf:
    def test_a_workday_tenant_belonging_to_someone_else_is_the_acquirer(self, R):
        assert R.acquirer_of("Paciolan", "paciolan", "workday",
                             "learfield|wd5|Paciolan") == "learfield"

    def test_lever_to_workday_acquisition(self, R):
        assert R.acquirer_of("AdColony", "adcolony", "workday",
                             "digitalturbine|wd501|DT_Careers") == "digitalturbine"

    def test_the_companys_own_new_board_is_not_an_acquisition(self, R):
        assert R.acquirer_of("Acme Labs", "acmelabs", "workday",
                             "acmelabs|wd1|External") == ""
        assert R.acquirer_of("Acme Labs", "acme-labs", "greenhouse", "acmelabs") == ""

    def test_a_substring_relationship_either_way_counts_as_the_same_company(self, R):
        assert R.acquirer_of("Acme", "acme", "greenhouse", "acmelabs") == ""
        assert R.acquirer_of("Acme Labs Holdings", "acmelabsholdings",
                             "greenhouse", "acme") == ""

    def test_matching_on_the_old_slug_is_enough(self, R):
        """The audit's `name` is sometimes just the slug; the slug still says
        whose board it is."""
        assert R.acquirer_of("", "acmelabs", "greenhouse", "acmelabs") == ""

    def test_an_empty_new_slug_has_no_acquirer(self, R):
        assert R.acquirer_of("Acme", "acme", "greenhouse", "") == ""


# --- the output row ---------------------------------------------------------

class TestResultRow:
    def _row(self):
        return {"ats": "greenhouse", "slug": "paciolan", "name": "Paciolan",
                "status": "dead"}

    def test_column_count_and_order(self, R):
        out = R.Outcome(old_status="dead")
        row = R.result_row(self._row(), out)
        assert len(row) == len(R.COLUMNS)
        assert R.COLUMNS == ["ats", "slug", "name", "old_status", "verdict",
                             "new_ats", "new_slug", "new_jobs", "acquirer",
                             "confidence", "domain", "evidence"]

    def test_a_gone_row_carries_no_proposed_addition(self, R):
        row = R.result_row(self._row(), R.Outcome(old_status="dead", web_checked=True))
        assert row[R.COLUMNS.index("verdict")] == R.GONE
        assert row[R.COLUMNS.index("new_slug")] == ""

    def test_a_moved_row_carries_the_new_board_and_the_acquirer(self, R):
        out = R.Outcome(old_status="dead")
        out.found = R.Candidate("workday", "learfield|wd5|Paciolan", 12, "tier2_name")
        out.evidence = ["tier2_name:workday/learfield|wd5|Paciolan=12"]
        row = R.result_row(self._row(), out)
        assert row[R.COLUMNS.index("verdict")] == R.MOVED
        assert row[R.COLUMNS.index("new_ats")] == "workday"
        assert row[R.COLUMNS.index("new_slug")] == "learfield|wd5|Paciolan"
        assert row[R.COLUMNS.index("new_jobs")] == 12
        assert row[R.COLUMNS.index("acquirer")] == "learfield"

    def test_evidence_is_deduped_and_carries_the_errors(self, R):
        out = R.Outcome(old_status="dead", web_checked=True)
        out.evidence = ["no_domain", "no_domain"]
        out.errors = ["ashby/acme:403"]
        evidence = R.result_row(self._row(), out)[R.COLUMNS.index("evidence")]
        assert evidence == "no_domain;err:ashby/acme:403"

    def test_the_domain_is_reported_on_alive_no_board(self, R):
        out = R.Outcome(old_status="dead", domain="paciolan.com")
        out.evidence = ["site_up_no_board"]
        row = R.result_row(self._row(), out)
        assert row[R.COLUMNS.index("verdict")] == R.ALIVE_NO_BOARD
        assert row[R.COLUMNS.index("domain")] == "paciolan.com"


# --- board endpoints --------------------------------------------------------

class TestBoardEndpoints:
    def test_the_probe_urls_are_lighter_than_the_scrapers(self, R):
        assert "content=true" not in R.board_url("greenhouse", "acme")
        assert "includeCompensation" not in R.board_url("ashby", "acme")

    def test_workday_has_no_get_url(self, R):
        with pytest.raises(ValueError):
            R.board_url("workday", "acme|wd1|X")

    @pytest.mark.parametrize("ats,payload,expected", [
        ("greenhouse", {"jobs": [{}, {}]}, 2),
        ("ashby", {"jobs": []}, 0),
        ("lever", [{}, {}, {}], 3),
        ("workday", {"total": 240, "jobPostings": [{}]}, 240),
        ("workday", {"total": 0, "jobPostings": [{}, {}]}, 2),
    ])
    def test_posting_count(self, R, ats, payload, expected):
        assert R.posting_count(ats, payload) == expected

    @pytest.mark.parametrize("ats,payload", [
        ("greenhouse", {"jobs": "nope"}),
        ("greenhouse", []),
        ("lever", {"jobs": []}),
        ("workday", {"total": 3}),
    ])
    def test_a_malformed_200_raises_rather_than_reading_as_empty(self, R, ats, payload):
        with pytest.raises(TypeError):
            R.posting_count(ats, payload)


# --- board identity ---------------------------------------------------------

class TestSameBoard:
    def test_a_different_lane_is_a_different_board(self, R):
        assert not R.same_board("greenhouse", "acme", "lever", "acme")

    def test_case_insensitive_by_default(self, R):
        assert R.same_board("lever", "Fliff", "lever", "fliff")

    def test_case_sensitive_for_a_levers_404(self, R):
        assert not R.same_board("lever", "Fliff", "lever", "fliff", case_sensitive=True)

    def test_identical_slugs(self, R):
        assert R.same_board("ashby", "0g", "ashby", "0g", case_sensitive=True)


class TestStatusSets:
    def test_the_statuses_the_audit_can_emit(self, R):
        assert set(R.INPUT_STATUSES) == {"active", "stale", "empty", "dead"}

    def test_live_and_not_found_do_not_overlap(self, R):
        assert not (R.LIVE_STATUSES & R.NOT_FOUND_STATUSES)
        assert R.LIVE_STATUSES | R.NOT_FOUND_STATUSES <= set(R.INPUT_STATUSES)

    def test_the_supported_lanes_are_the_registrys(self, R):
        from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES

        assert set(R.SUPPORTED_ATS) == set(ATS_SOURCE_NAMES)


# --- the domain-existence corpus --------------------------------------------

# Hand-verified from a live 50-row sample where 19 rows read `gone`: every one
# of these companies resolves in DNS and is trading. Each column is the audit
# slug, the audit name, and the domain the resolver has to be able to guess.
GONE_FALSE_POSITIVES = [
    ("350org", "350org", "350.org"),
    ("21co", "21co", "21.co"),
    ("2seventybioinc", "2seventybioinc", "2seventybio.com"),
    ("abtasty", "abtasty", "abtasty.com"),
    ("777partners", "777partners", "777partners.com"),
    ("1848ventures", "1848ventures", "1848ventures.com"),
    ("54collective", "54collective", "54collective.com"),
    ("10up-2", "10up-2", "10up.com"),
]


class TestDomainExistenceCorpus:
    @pytest.mark.parametrize("slug,name,domain", GONE_FALSE_POSITIVES)
    def test_the_real_domain_is_guessed(self, R, slug, name, domain):
        assert domain in R.domain_candidates(name, slug)

    @pytest.mark.parametrize("slug,name,domain", GONE_FALSE_POSITIVES)
    def test_a_resolving_domain_is_never_gone(self, R, slug, name, domain):
        """Whatever HTTP then does -- 403, 404, timeout -- DNS settled it."""
        out = R.Outcome(old_status="dead", web_checked=True, domain=domain)
        verdict, _ = R.decide_verdict(out)
        assert verdict == R.ALIVE_NO_BOARD
        assert not R.is_retireable(verdict)


class TestStemSplits:
    @pytest.mark.parametrize("stem,expected", [
        ("21co", "21.co"),
        ("wisecarbonio", "wisecarbon.io"),
        ("acmeai", "acme.ai"),
        ("350org", "350.org"),
        ("acmecom", "acme.com"),   # the longest TLD wins over `co`
        ("acmelabs", ""),
        ("co", ""),
    ])
    def test_split_tld_stem(self, R, stem, expected):
        assert R.split_tld_stem(stem) == expected

    @pytest.mark.parametrize("stem,expected", [
        ("2seventybioinc", "2seventybio"),
        ("acmelabsllc", "acmelabs"),
        ("acmeholdings", "acme"),
        ("acmelabs", ""),
        ("inc", ""),
    ])
    def test_strip_glued_suffix(self, R, stem, expected):
        assert R.strip_glued_suffix(stem) == expected

    @pytest.mark.parametrize("stem,expected", [
        ("10up-2", "10up"),
        ("acme-3", "acme"),
        ("acme-careers", "acme"),
        ("acme-jobs", "acme"),
        ("acme-inc", "acme"),
        ("acme-hq", "acme"),
        ("acmecareers", "acme"),
        ("acmejobs", "acme"),
        ("acme", ""),
    ])
    def test_strip_board_suffix(self, R, stem, expected):
        assert R.strip_board_suffix(stem) == expected

    def test_both_the_glued_and_the_stripped_form_are_candidates(self, R):
        got = R.domain_candidates("2seventybioinc", "2seventybioinc")
        assert "2seventybio.com" in got and "2seventybioinc.com" in got

    def test_the_widened_tld_list_keeps_the_originals(self, R):
        for tld in ("com", "io", "ai", "co", "so"):
            assert tld in R.TLDS
        for tld in ("org", "net", "dev", "app", "xyz", "tech", "co.uk", "de",
                    "fr", "nl", "se", "com.br", "in", "health", "finance"):
            assert tld in R.TLDS

    def test_every_stem_gets_its_com_before_any_long_tail_tld(self, R):
        """The per-row cap makes ordering load-bearing: a fifth stem's `.com`
        is a better guess than the first stem's `.xyz`."""
        got = R.domain_candidates("2seventybioinc", "2seventybioinc")
        last_com = max(i for i, d in enumerate(got) if d.endswith(".com"))
        first_tail = min(i for i, d in enumerate(got) if d.endswith(".xyz"))
        assert last_com < first_tail


class TestSiteReadConfidence:
    def test_a_domain_we_read_is_high_confidence(self, R):
        out = R.Outcome(old_status="dead", web_checked=True,
                        domain="acme.com", site_read=True)
        assert R.decide_verdict(out) == (R.ALIVE_NO_BOARD, R.HIGH)

    def test_a_domain_that_only_resolved_is_low_confidence(self, R):
        out = R.Outcome(old_status="dead", web_checked=True, domain="acme.com")
        assert R.decide_verdict(out) == (R.ALIVE_NO_BOARD, R.LOW)

    def test_neither_is_retireable(self, R):
        for site_read in (False, True):
            out = R.Outcome(old_status="dead", web_checked=True,
                            domain="acme.com", site_read=site_read)
            assert not R.is_retireable(R.decide_verdict(out)[0])


# --- raw DNS packets --------------------------------------------------------

class TestDnsPackets:
    def test_the_query_is_a_recursion_desired_soa_question(self, R):
        p = R.dns_query_packet("21.co", 0xBEEF)
        assert p[:2] == b"\xbe\xef"
        assert p[2:4] == b"\x01\x00"          # RD set
        assert p[4:6] == b"\x00\x01"          # one question
        assert p[12:] == b"\x02\x32\x31\x02\x63\x6f\x00\x00\x06\x00\x01"

    def test_a_subdomain_is_labelled_per_dot(self, R):
        p = R.dns_query_packet("jobs.acme.co.uk", 1)
        assert p[12:].startswith(b"\x04jobs\x04acme\x02co\x02uk\x00")

    def _resp(self, query_id, rcode):
        return bytes([query_id >> 8, query_id & 0xFF, 0x81, 0x80 | rcode]) + bytes(8)

    def test_noerror_means_the_name_exists(self, R):
        """Including a no-address answer: `21.co` is registered with no A."""
        assert R.dns_response_state(self._resp(7, 0), 7) == R.DNS_OK

    def test_nxdomain_is_the_only_absence(self, R):
        assert R.dns_response_state(self._resp(7, 3), 7) == R.DNS_NXDOMAIN

    @pytest.mark.parametrize("rcode", [1, 2, 4, 5])
    def test_every_other_rcode_is_unknown(self, R, rcode):
        assert R.dns_response_state(self._resp(7, rcode), 7) == R.DNS_UNKNOWN

    def test_a_mismatched_id_is_unknown_not_absence(self, R):
        assert R.dns_response_state(self._resp(9, 3), 7) == R.DNS_UNKNOWN

    def test_a_short_packet_is_unknown(self, R):
        assert R.dns_response_state(b"", 7) == R.DNS_UNKNOWN
        assert R.dns_response_state(b"\x00" * 11, 0) == R.DNS_UNKNOWN


# --- the name-mangling corpus -----------------------------------------------

# Hand-checked `gone` rows from a live 150-row dead-slug sample. Every failure
# is the same class: the audit slug is the company name with a board-segment
# suffix glued on, often truncated at the board's width limit, sometimes with
# punctuation spelled out. The name column is the slug itself -- the worst case,
# where the dump gives the resolver nothing the slug does not already carry.
MANGLED_SLUGS = [
    ("pjtpartnersprofessionals", "pjtpartners.com"),
    ("evolutionparkingguestservices", "evolutionparking.com"),
    ("roundpointmortgageservicingcor", "roundpointmortgage.com"),
    ("halodotscience", "halo.science"),
    ("centerforhumancompatibleartifi", "humancompatible.ai"),
    ("shrapnelstudio", "shrapnel.com"),
    # Two companies in one slug. The leading one is enough: any candidate that
    # resolves blocks the retirement, so the trailing `redcanary.com` -- a
    # suffix, which no prefix sweep reaches -- is not needed.
    ("zscalerredcanary", "zscaler.com"),
]


class TestMangledSlugCorpus:
    @pytest.mark.parametrize("slug,domain", MANGLED_SLUGS)
    def test_the_real_domain_is_guessed(self, R, slug, domain):
        assert domain in R.domain_candidates(slug, slug)

    @pytest.mark.parametrize("slug,domain", MANGLED_SLUGS)
    def test_the_real_name_shortens_it_rather_than_breaking_it(self, R, slug, domain):
        """The audit name is usually cleaner than the slug, never worse."""
        name = domain.rsplit(".", 1)[0]
        assert domain in R.domain_candidates(name, slug)

    @pytest.mark.parametrize("slug,domain", MANGLED_SLUGS)
    def test_a_resolving_candidate_is_never_gone(self, R, slug, domain):
        out = R.Outcome(old_status="dead", web_checked=True, domain=domain)
        verdict, _ = R.decide_verdict(out)
        assert verdict == R.ALIVE_NO_BOARD
        assert not R.is_retireable(verdict)

    @pytest.mark.parametrize("slug,domain", MANGLED_SLUGS)
    def test_the_candidate_list_stays_within_the_cap(self, R, slug, domain):
        got = R.domain_candidates(slug, slug)
        assert len(got) == len(set(got)) <= R.MAX_DOMAIN_CANDIDATES


class TestDotDecoding:
    @pytest.mark.parametrize("stem,expected", [
        ("halodotscience", "halo.science"),
        ("acmedotcom", "acme.com"),
        ("acmedotcoduk", "acme.coduk"),
        ("dotdash", ""),            # no usable `dot` inside
        ("acmedot", ""),            # nothing after it
        ("adotio", ""),             # one character of brand
        ("acmelabs", ""),
    ])
    def test_decode_dot_domain(self, R, stem, expected):
        assert R.decode_dot_domain(stem) == expected

    def test_the_decoded_domain_bypasses_the_tld_list(self, R):
        """`science` is not in TLDS; the slug already spelled the whole name."""
        assert "science" not in R.TLDS
        assert "halo.science" in R.domain_candidates("halodotscience", "halodotscience")

    def test_the_halves_are_stems_too(self, R):
        assert R.dot_stems("halodotscience") == ["halo", "haloscience"]
        got = R.domain_candidates("halodotscience", "halodotscience")
        assert "halo.com" in got and "haloscience.com" in got

    def test_nothing_to_decode_yields_no_stems(self, R):
        assert R.dot_stems("acmelabs") == []


class TestSegmentSuffixes:
    @pytest.mark.parametrize("stem,expected", [
        ("pjtpartnersprofessionals", ["pjtpartners"]),
        ("evolutionparkingguestservices", ["evolutionparking"]),
        ("shrapnelstudio", ["shrapnel"]),
        ("acmeholdings", ["acme"]),
        ("acmecorporate", ["acme"]),
        ("acmeschoolyear", ["acme"]),
        ("studios", []),            # nothing plausible would remain
        ("acmelabs", ["acme"]),
    ])
    def test_strip_segment_suffixes(self, R, stem, expected):
        assert R.strip_segment_suffixes(stem) == expected

    def test_the_longest_matching_suffix_wins(self, R):
        assert R.strip_segment_suffixes("roundpointservicingcorp") == ["roundpoint"]

    def test_stripping_stops_before_it_eats_the_brand(self, R):
        """`pjt` is under SEGMENT_STEM_MIN, so the second pass does not fire."""
        assert "pjt" not in R.strip_segment_suffixes("pjtpartnersprofessionals")

    def test_the_word_list_covers_what_the_sample_showed(self, R):
        for word in ("professionals", "guestservices", "servicing", "studio",
                     "studios", "team", "teams", "careers", "jobs", "hiring",
                     "talent", "recruiting", "contractors", "interns",
                     "internships", "students", "alumni", "campus",
                     "corporate", "external", "employees", "staff", "temp",
                     "seasonal", "schoolyear", "portfolio", "group",
                     "holdings", "labs", "ventures", "partners"):
            assert word in R._SEGMENT_SUFFIXES


class TestLeadingWords:
    @pytest.mark.parametrize("stem,expected", [
        ("centerforhumancompatibleartifi", "humancompatibleartifi"),
        ("theacmefoundation", "acmefoundation"),
        ("universityofsomewhere", "somewhere"),
        ("centerpoint", ""),        # stripping would leave too little
        ("acmelabs", ""),
    ])
    def test_strip_leading_word(self, R, stem, expected):
        assert R.strip_leading_word(stem) == expected


class TestTruncation:
    def test_a_slug_at_the_width_limit_is_treated_as_truncated(self, R):
        assert R.SLUG_TRUNCATION_LEN == 30
        assert R.is_truncated_slug("roundpointmortgageservicingcor")
        assert R.is_truncated_slug("centerforhumancompatibleartifi")

    def test_a_shorter_slug_is_not(self, R):
        assert not R.is_truncated_slug("acmelabs")
        assert not R.is_truncated_slug("")

    def test_a_workday_slug_is_judged_on_its_tenant(self, R):
        assert not R.is_truncated_slug("learfield|wd5|Paciolan")

    def test_truncation_buys_a_deeper_sweep(self, R):
        stem = "roundpointmortgageservicingcor"
        assert (len(R.length_prefixes(stem, truncated=True))
                > len(R.length_prefixes(stem, truncated=False)))
        assert "roundpointmortgage" in R.length_prefixes(stem, truncated=True)


class TestPrefixSweep:
    def test_prefixes_are_longest_first(self, R):
        got = R.length_prefixes("zscalerredcanary")
        assert got == sorted(got, key=len, reverse=True)

    def test_nothing_under_the_minimum_length(self, R):
        for p in R.length_prefixes("zscalerredcanary", truncated=True):
            assert len(p) >= R.PREFIX_MIN_LEN
        assert R.length_prefixes("acme") == []

    def test_the_whole_stem_is_not_its_own_prefix(self, R):
        assert "zscalerredcanary" not in R.length_prefixes("zscalerredcanary")

    def test_each_cap_is_honoured(self, R):
        long_stem = "a" * 60
        assert len(R.length_prefixes(long_stem)) == R.MAX_PREFIXES_PER_STEM
        assert (len(R.length_prefixes(long_stem, truncated=True))
                == R.MAX_PREFIXES_TRUNCATED)
        assert len(R.prefix_bases(long_stem, long_stem)) <= R.MAX_PREFIX_BASES

    def test_word_boundaries_come_first(self, R):
        got = R.prefix_bases("RoundPoint Mortgage Servicing Corp",
                             "roundpointmortgageservicingcor")
        assert got[:2] == ["roundpoint", "roundpointmortgage"]

    def test_the_whole_name_is_not_a_word_prefix(self, R):
        """`domain_stems` already carries it; repeating it wastes a lookup."""
        assert "acmelabs" not in R.word_prefixes("Acme Labs")
        assert R.word_prefixes("Acme") == []

    def test_a_stem_too_short_to_guess_still_yields_nothing(self, R):
        assert R.domain_candidates("0g", "0g") == []
        assert R.prefix_bases("0g", "0g") == []


class TestPrefixOrdering:
    def test_prefixes_only_get_the_head_of_the_tld_list(self, R):
        assert R.PREFIX_TLDS == ("com", "io", "ai")
        got = R.domain_candidates("zscalerredcanary", "zscalerredcanary")
        assert "zscaler.com" in got
        assert "zscaler.xyz" not in got

    def test_a_prefix_com_still_beats_a_full_stem_long_tail(self, R):
        """The per-row cap makes ordering load-bearing in both directions."""
        got = R.domain_candidates("zscalerredcanary", "zscalerredcanary")
        assert got.index("zscaler.com") < got.index("zscalerredcanary.xyz")

    def test_the_full_stem_com_still_comes_first(self, R):
        got = R.domain_candidates("shrapnelstudio", "shrapnelstudio")
        assert got.index("shrapnelstudio.com") < got.index("shrapnel.com")
