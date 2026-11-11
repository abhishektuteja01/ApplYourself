"""parse.py: HTML, dates, salary and JobPosting blocks."""

from __future__ import annotations

from datetime import date

import pytest

from applyourself.discovery import parse

# ---------- html and dates ----------


def test_html_drops_script_bodies():
    text = parse.html_to_text("<p>Build things</p><script>var x = 1;</script><li>ship</li>")
    assert "var x" not in text and "Build things" in text and "- ship" in text


def test_html_decodes_double_encoding():
    assert parse.html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"


def test_dates_are_utc():
    assert parse.iso_date("2026-09-27T23:30:00-05:00") == date(2026, 9, 28)
    assert parse.ms_date(0) == date(1970, 1, 1)
    assert parse.ms_date(True) is None


def test_relative_dates():
    today = date(2026, 9, 27)
    assert parse.relative_posted_date("Posted Today", today) == today
    assert parse.relative_posted_date("Posted 30+ Days Ago", today) == date(2026, 8, 28)
    assert parse.relative_posted_date("soon", today) is None


# ---------- field parsers every source shares ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        ("yes", True),
        ("0", False),
        (1, True),
        (0, False),
        (None, False),
        ("", False),
        (float("nan"), False),
    ],
)
def test_as_bool(value, expected):
    assert parse.as_bool(value) is expected


@pytest.mark.parametrize(
    "amount,period,expected",
    [
        (80, "HOUR", 166_400),
        (80.0, "hourly", 166_400),
        (10_000, "MONTH", 120_000),
        (500, "DAY", 130_000),
        (2_000, "per-week", 104_000),
        (120_000, "YEAR", 120_000),
        (120_000, "", 120_000),
        (120_000, None, 120_000),
        ("84.0", "HOUR", 174_720),
        (None, "HOUR", None),
        ("n/a", "YEAR", None),
    ],
)
def test_yearly_salary(amount, period, expected):
    assert parse.yearly_salary(amount, period) == expected


def _page(payload: str) -> str:
    return (
        f"<html><head><script type='application/ld+json'>{payload}</script></head>"
        "<body><nav>Jobs Companies Log in</nav></body></html>"
    )


def test_a_job_posting_block_gives_the_jd():
    desc = "<p>Build retrieval pipelines.</p><ul><li>Ship weekly</li></ul>"
    page = _page(
        '{"@context": "https://schema.org", "@type": "JobPosting", '
        f'"title": "AI Engineer", "description": "{desc}"}}'
    )
    assert parse.jobposting_text(page) == "Build retrieval pipelines.\n\n- Ship weekly"


def test_a_job_posting_inside_a_list_or_graph_is_found():
    inner = '{"@type": "JobPosting", "description": "Own the eval harness."}'
    assert (
        parse.jobposting_text(_page(f'[{{"@type": "WebSite"}}, {inner}]'))
        == "Own the eval harness."
    )
    assert parse.jobposting_text(_page(f'{{"@graph": [{inner}]}}')) == "Own the eval harness."


@pytest.mark.parametrize(
    "page",
    [
        "",
        "<html><body>No data</body></html>",
        _page("{not json"),
        _page('{"@type": "Organization", "description": "We make tools."}'),
    ],
)
def test_no_job_posting_block_gives_nothing(page):
    assert parse.jobposting_text(page) == ""


@pytest.mark.parametrize(
    "org,expected",
    [
        ('{"@type": "Organization", "name": "Lennox International"}', "Lennox International"),
        ('"Waters Corporation"', "Waters Corporation"),
        ('{"@type": "Organization", "name": "UNAVAILABLE"}', ""),
        ('{"@type": "Organization"}', ""),
    ],
)
def test_the_employer_comes_from_the_job_posting_block(org, expected):
    page = _page(f'{{"@type": "JobPosting", "description": "x", "hiringOrganization": {org}}}')
    assert parse.jobposting_employer(page) == expected


def test_no_block_no_employer():
    assert parse.jobposting_employer("<html></html>") == ""


@pytest.mark.parametrize(
    "name,slug",
    [
        ("whatfix101", True),
        ("novella", True),
        ("careers.pnc.com", True),
        ("appsilon-1739358905", True),
        ("Whatfix", False),
        ("DataDome", False),
        ("NTT DATA, Inc.", False),
        ("1KOMMA5°", False),
        ("小米", False),
        ("", False),
    ],
)
def test_looks_like_slug(name, slug):
    assert parse.looks_like_slug(name) is slug


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We raised $20 - $30 million. Salary $150k-$200k", (150000.0, 200000.0, "USD")),
        ("$5,000 - $10,000 signing bonus", None),
        ("$25,000 - $50,000 in equity. Base $120,000 - $140,000", (120000.0, 140000.0, "USD")),
        ("$150k-$200k plus bonus", (150000.0, 200000.0, "USD")),
        (
            "USD Salary Range: $160,000 - $210,000\nCAD Salary Range: $140,000 - $180,000",
            (160000.0, 210000.0, "USD"),
        ),
    ],
)
def test_posted_pay_skips_ranges_that_are_not_salary(text, expected):
    assert parse.posted_pay(text) == expected
