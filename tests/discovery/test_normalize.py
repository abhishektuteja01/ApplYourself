"""normalize.py: the job_id golden set, the title gate and posting keys.

The golden ids are frozen. A change here orphans every
pipeline/<job_id>/state.yaml, so a failing golden test is a stop, not a fixup.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.discovery import normalize as N


GOLDEN = [
    ("Anthropic", "Applied AI Engineer", "anthropic", "applied ai engineer", "1163cbe5"),
    ("OpenAI, Inc.", "Software Engineer, Codex", "openai", "software engineer codex", "291f16d1"),
    ("The Walt Disney Company", "Sr. ML Engineer", "walt disney company", "sr ml engineer", "e1e07af2"),
    ("Hilbert's AI via LinkedIn", "AI Engineer", "hilbert s ai", "ai engineer", "fd3ebae8"),
    ("Acme GmbH", "Data-Scientist (Remote)", "acme", "data scientist remote", "e1ad20f7"),
]


@pytest.mark.parametrize("company,title,cn,tn,_", GOLDEN)
def test_normalizers(company, title, cn, tn, _):
    assert N.normalize_company(company) == cn
    assert N.normalize_title(title) == tn


@pytest.mark.parametrize("company,title,_cn,_tn,job_id", GOLDEN)
def test_job_id_is_frozen(company, title, _cn, _tn, job_id):
    assert N.compute_job_id(N.normalize_company(company), N.normalize_title(title)) == job_id


# ---------- posting keys ----------

@pytest.mark.parametrize("a,b", [
    ("https://job-boards.greenhouse.io/anthropic/jobs/4020305008",
     "https://job-boards.greenhouse.io/anthropic/jobs/4020305008?src=LinkedIn&utm_source=x"),
    ("https://job-boards.greenhouse.io/anthropic/jobs/4020305008",
     "https://www.anthropic.com/careers?gh_jid=4020305008"),
    ("https://jobs.lever.co/palantir/636fc05c-d348-4a3a-9f0f-2e1a2b3c4d5e",
     "https://jobs.lever.co/palantir/636fc05c-d348-4a3a-9f0f-2e1a2b3c4d5e/apply?lever-source=LinkedIn"),
    ("https://www.linkedin.com/jobs/view/4464823374",
     "https://www.linkedin.com/jobs/view/ai-engineer-at-acme-4464823374/?trk=x"),
    ("https://www.indeed.com/viewjob?jk=7facac86385f1234",
     "https://ae.indeed.com/viewjob?jk=7facac86385f1234&from=serp"),
    ("https://careers.example.com/jobs/123/", "https://careers.example.com/jobs/123?src=LinkedIn"),
])
def test_same_posting_same_key(a, b):
    assert N.posting_key(a) == N.posting_key(b) != ""


@pytest.mark.parametrize("a,b", [
    ("https://4dmoleculartherapeutics.com/careers/?gh_jid=4724623005",
     "https://4dmoleculartherapeutics.com/careers/?gh_jid=4728433005"),
    ("https://ae.indeed.com/viewjob?jk=c5f00ee9d878c9ba",
     "https://ae.indeed.com/viewjob?jk=4bc1248e550b5af9"),
    ("https://careers.example.com/job?id=1", "https://careers.example.com/job?id=2"),
])
def test_id_params_keep_distinct_postings_apart(a, b):
    assert N.posting_key(a) != N.posting_key(b)


def test_workday_keys_on_tenant_and_req():
    a = "https://cigna.wd5.myworkdayjobs.com/cignacareers/job/Austin-TX/AI-Engineer_R123"
    b = "https://cigna.wd5.myworkdayjobs.com/External/job/Remote/AI-Engineer_R123?source=x"
    c = "https://cigna.wd5.myworkdayjobs.com/en-US/cignacareers/job/AI-Engineer_R123"
    assert N.posting_key(a) == N.posting_key(b) == N.posting_key(c) == "workday|cigna|ai-engineer_r123"


def test_blank_url_has_no_key():
    assert N.posting_key("") == "" and N.posting_key(None) == ""


# ---------- html and dates ----------

def test_html_drops_script_bodies():
    text = N.html_to_text("<p>Build things</p><script>var x = 1;</script><li>ship</li>")
    assert "var x" not in text and "Build things" in text and "- ship" in text


def test_html_decodes_double_encoding():
    assert N.html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"


def test_dates_are_utc():
    assert N.iso_date("2026-09-27T23:30:00-05:00") == date(2026, 9, 28)
    assert N.ms_date(0) == date(1970, 1, 1)
    assert N.ms_date(True) is None


def test_relative_dates():
    today = date(2026, 9, 27)
    assert N.relative_posted_date("Posted Today", today) == today
    assert N.relative_posted_date("Posted 30+ Days Ago", today) == date(2026, 8, 28)
    assert N.relative_posted_date("soon", today) is None


# ---------- title gate ----------

def test_gate_exempts_manual_and_unknown_lanes(cfg):
    assert N.title_passes("Director of Anything", "no_such_lane")
    assert N.title_passes("Senior Director", cfg.default_vertical, source="manual")


def test_company_from_url():
    assert N.company_from_url("https://job-boards.greenhouse.io/edison-scientific/jobs/1") \
        == "Edison Scientific"
    assert N.company_from_url("https://www.linkedin.com/jobs/view/1") == ""
    assert N.company_from_url("https://job-boards.greenhouse.io/nc/jobs/1") == ""


# ---------- field parsers every source shares ----------

@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), ("true", True), ("false", False), ("True", True),
    ("FALSE", False), ("yes", True), ("0", False), (1, True), (0, False),
    (None, False), ("", False), (float("nan"), False),
])
def test_as_bool(value, expected):
    assert N.as_bool(value) is expected


@pytest.mark.parametrize("amount,period,expected", [
    (80, "HOUR", 166_400), (80.0, "hourly", 166_400), (10_000, "MONTH", 120_000),
    (500, "DAY", 130_000), (2_000, "per-week", 104_000), (120_000, "YEAR", 120_000),
    (120_000, "", 120_000), (120_000, None, 120_000), ("84.0", "HOUR", 174_720),
    (None, "HOUR", None), ("n/a", "YEAR", None),
])
def test_yearly_salary(amount, period, expected):
    assert N.yearly_salary(amount, period) == expected


@pytest.mark.parametrize("name", ["siehe Beschreibung", "Non renseigné", "  NON RENSEIGNE "])
def test_a_placeholder_company_is_no_company(name):
    assert N.normalize_company(name) == ""


def _page(payload: str) -> str:
    return (f"<html><head><script type='application/ld+json'>{payload}</script></head>"
            "<body><nav>Jobs Companies Log in</nav></body></html>")


def test_a_job_posting_block_gives_the_jd():
    desc = "<p>Build retrieval pipelines.</p><ul><li>Ship weekly</li></ul>"
    page = _page('{"@context": "https://schema.org", "@type": "JobPosting", '
                 f'"title": "AI Engineer", "description": "{desc}"}}')
    assert N.jobposting_text(page) == "Build retrieval pipelines.\n\n- Ship weekly"


def test_a_job_posting_inside_a_list_or_graph_is_found():
    inner = '{"@type": "JobPosting", "description": "Own the eval harness."}'
    assert N.jobposting_text(_page(f'[{{"@type": "WebSite"}}, {inner}]')) == "Own the eval harness."
    assert N.jobposting_text(_page(f'{{"@graph": [{inner}]}}')) == "Own the eval harness."


@pytest.mark.parametrize("page", [
    "", "<html><body>No data</body></html>", _page("{not json"),
    _page('{"@type": "Organization", "description": "We make tools."}'),
])
def test_no_job_posting_block_gives_nothing(page):
    assert N.jobposting_text(page) == ""
