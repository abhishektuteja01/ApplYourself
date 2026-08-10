"""Posting URL -> rendered form -> board slug.

The URL shapes are the ones that actually appear in clean.parquet: 122 of the
155 Greenhouse rows are the board path form, and 32 are a company careers page
carrying only `?gh_jid=` with the slug nowhere in sight. That second group is
why nothing here parses a slug out of a URL — the token is fetched on its own
and the slug is read back off the rendered form, which was verified against 58
live postings.

No network: fetch_text and fetch_questions are both stubbed.
"""
from __future__ import annotations

import pytest

from src.apply import greenhouse as G
from src.apply.greenhouse import (
    ApplyUrlError,
    PostingExpired,
    fetch_form,
    load_board,
    parse_posting,
    slug_from_form,
)
from src.apply.schema import parse_schema
from src.discovery.sources.ats.http import CareersError

from .conftest import FORM_FIXTURES, load_fixture, load_html

# Slug and token as they are scrubbed into each committed fixture pair.
FIXTURE_BOARDS = {
    "form_minimal": ("gasketworks", "1000001"),
    "form_multiselect": ("ratchetco", "1000002"),
    "form_education": ("bushinggroup", "1000003"),
    "form_demographic": ("bearingenergy", "1000004"),
    "form_employment": ("flywheelsystems", "1000005"),
}


class TestParsePosting:
    @pytest.mark.parametrize("url,slug,token", [
        # Every shape below is verbatim from clean.parquet.
        ("https://job-boards.greenhouse.io/asteralabs/jobs/4719162005",
         "asteralabs", "4719162005"),
        ("https://boards.greenhouse.io/accenturefederalservices/jobs/4699491006?gh_jid=4699491006",
         "accenturefederalservices", "4699491006"),
        ("https://job-boards.eu.greenhouse.io/someco/jobs/123", "someco", "123"),
    ])
    def test_board_path_form(self, url, slug, token):
        posting = parse_posting(url)
        assert (posting.url_slug, posting.token) == (slug, token)

    @pytest.mark.parametrize("url,token", [
        ("https://www.hubspot.com/careers/jobs/8084478?gh_jid=8084478", "8084478"),
        ("https://careers.ovo.com/?gh_jid=8088319", "8088319"),
        ("https://www.workato.com/careers?gh_jid=8112909002#open-roles", "8112909002"),
        ("http://bankrate.com/careers/current-openings?gh_jid=8099793", "8099793"),
        # One live row spells the same id twice.
        ("https://jobs.elastic.co/jobs?gh_jid=8079636&gh_jid=8079636", "8079636"),
    ])
    def test_company_page_with_gh_jid_and_no_slug(self, url, token):
        posting = parse_posting(url)
        assert posting.token == token
        assert posting.url_slug is None

    def test_board_query_param_is_read_as_the_slug(self):
        posting = parse_posting(
            "https://coreweave.com/careers/job?4703200006&board=coreweave&gh_jid=4703200006"
        )
        assert (posting.url_slug, posting.token) == ("coreweave", "4703200006")

    def test_two_different_gh_jids_is_an_error_not_a_coin_flip(self):
        with pytest.raises(ApplyUrlError, match="2 different"):
            parse_posting("https://x.com/jobs?gh_jid=111&gh_jid=222")

    def test_non_numeric_gh_jid_rejected(self):
        with pytest.raises(ApplyUrlError, match="not numeric"):
            parse_posting("https://x.com/jobs?gh_jid=abc")

    @pytest.mark.parametrize("url", [
        "https://www.linkedin.com/jobs/view/4422512798",
        "https://jobs.ashbyhq.com/farsight/820c851b-ae40-45a7-af9b-03ec087aff6c",
        "https://jobs.lever.co/someco/abc-123",
    ])
    def test_other_boards_are_rejected(self, url):
        with pytest.raises(ApplyUrlError, match="not a Greenhouse"):
            parse_posting(url)

    def test_greenhouse_url_with_no_token_says_so(self):
        with pytest.raises(ApplyUrlError, match="no job token"):
            parse_posting("https://job-boards.greenhouse.io/asteralabs")

    @pytest.mark.parametrize("url", ["", "   ", None])
    def test_empty_url(self, url):
        with pytest.raises(ApplyUrlError):
            parse_posting(url)

    def test_form_url_carries_the_token_and_no_slug(self):
        posting = parse_posting("https://job-boards.greenhouse.io/asteralabs/jobs/4719162005")
        assert posting.form_url == (
            "https://boards.greenhouse.io/embed/job_app?token=4719162005"
        )
        assert "for=" not in posting.form_url


class TestSlugFromForm:
    @pytest.mark.parametrize("name", FORM_FIXTURES)
    def test_every_captured_form_yields_its_slug(self, name):
        slug, token = FIXTURE_BOARDS[name]
        assert slug_from_form(load_html(name), token) == slug

    def test_a_form_for_another_token_is_refused(self):
        # A response holding some other posting's form would fill this role's
        # application with another role's questions.
        with pytest.raises(CareersError, match="not 999"):
            slug_from_form(load_html("form_minimal"), "999")

    @pytest.mark.parametrize("html", ["", "<html><body>gone</body></html>",
                                      '<form action="/somewhere/else"></form>'])
    def test_no_readable_action_raises(self, html):
        with pytest.raises(CareersError, match="board slug"):
            slug_from_form(html, "1000001")


@pytest.fixture
def stub(monkeypatch):
    """Serve one canned form and one canned schema payload."""
    def install(name="form_minimal", *, text_exc=None):
        calls = {"text": [], "questions": []}

        def fake_text(url, timeout=30, deadline_ts=None):
            calls["text"].append(url)
            if text_exc is not None:
                raise text_exc
            return load_html(name)

        def fake_questions(slug, token, timeout=30):
            calls["questions"].append((slug, token))
            return parse_schema(load_fixture(name))

        monkeypatch.setattr(G, "fetch_text", fake_text)
        monkeypatch.setattr(G, "fetch_questions", fake_questions)
        return calls

    return install


class TestFetchForm:
    def test_404_is_an_expired_posting_not_a_crash(self, stub):
        # 3 of 58 sampled live postings were already gone.
        stub(text_exc=CareersError("gone", status=404, permanent=True))
        with pytest.raises(PostingExpired, match="4719162005"):
            fetch_form(parse_posting(
                "https://job-boards.greenhouse.io/x/jobs/4719162005"
            ))

    def test_other_fetch_failures_propagate(self, stub):
        stub(text_exc=CareersError("HTTP 503", status=503))
        with pytest.raises(CareersError, match="503"):
            fetch_form(parse_posting("https://job-boards.greenhouse.io/x/jobs/1"))


class TestLoadBoard:
    def test_fetches_by_token_and_enriches_with_the_recovered_slug(self, stub):
        calls = stub("form_minimal")
        board = load_board("https://www.hubspot.com/careers/jobs/1000001?gh_jid=1000001")

        assert calls["text"] == [
            "https://boards.greenhouse.io/embed/job_app?token=1000001"
        ]
        # The slug the API is asked for came from the form, not the URL.
        assert calls["questions"] == [("gasketworks", "1000001")]
        assert board.slug == "gasketworks"
        assert board.posting.url_slug is None

    @pytest.mark.parametrize("name", FORM_FIXTURES)
    def test_every_fixture_pair_loads_to_a_reconciled_form(self, stub, name):
        slug, token = FIXTURE_BOARDS[name]
        stub(name)
        board = load_board(f"https://job-boards.greenhouse.io/{slug}/jobs/{token}")
        assert board.reconciled.fields
        assert board.scan.submit_selector

    def test_a_url_slug_that_disagrees_with_the_form_raises(self, stub):
        # Never seen live (0 of 22 sampled), and a silent swap would apply to
        # the wrong company's posting.
        stub("form_minimal")
        with pytest.raises(CareersError, match="resolves to"):
            load_board("https://job-boards.greenhouse.io/someoneelse/jobs/1000001")

    def test_url_slug_case_difference_is_not_a_disagreement(self, stub):
        stub("form_minimal")
        board = load_board("https://job-boards.greenhouse.io/GasketWorks/jobs/1000001")
        assert board.slug == "gasketworks"
