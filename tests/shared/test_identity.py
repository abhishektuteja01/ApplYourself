"""identity.py: the job_id golden set and posting keys.

The golden ids are frozen. A change here orphans every
pipeline/<job_id>/state.yaml, so a failing golden test is a stop, not a fixup.
"""
from __future__ import annotations

import pytest

from src.shared import identity
from src.shared.job_boards import ATS_SOURCE_NAMES, board_slug, is_applyable

GOLDEN = [
    ("Anthropic", "Applied AI Engineer", "anthropic", "applied ai engineer", "1163cbe5"),
    ("OpenAI, Inc.", "Software Engineer, Codex", "openai", "software engineer codex", "291f16d1"),
    ("The Walt Disney Company", "Sr. ML Engineer", "walt disney company", "sr ml engineer", "e1e07af2"),
    ("Hilbert's AI via LinkedIn", "AI Engineer", "hilbert s ai", "ai engineer", "fd3ebae8"),
    ("Acme GmbH", "Data-Scientist (Remote)", "acme", "data scientist remote", "e1ad20f7"),
]


@pytest.mark.parametrize("company,title,cn,tn,_", GOLDEN)
def test_normalizers(company, title, cn, tn, _):
    assert identity.normalize_company(company) == cn
    assert identity.normalize_title(title) == tn


@pytest.mark.parametrize("company,title,_cn,_tn,job_id", GOLDEN)
def test_job_id_is_frozen(company, title, _cn, _tn, job_id):
    assert identity.compute_job_id(identity.normalize_company(company), identity.normalize_title(title)) == job_id


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
    assert identity.posting_key(a) == identity.posting_key(b) != ""


@pytest.mark.parametrize("a,b", [
    ("https://4dmoleculartherapeutics.com/careers/?gh_jid=4724623005",
     "https://4dmoleculartherapeutics.com/careers/?gh_jid=4728433005"),
    ("https://ae.indeed.com/viewjob?jk=c5f00ee9d878c9ba",
     "https://ae.indeed.com/viewjob?jk=4bc1248e550b5af9"),
    ("https://careers.example.com/job?id=1", "https://careers.example.com/job?id=2"),
])
def test_id_params_keep_distinct_postings_apart(a, b):
    assert identity.posting_key(a) != identity.posting_key(b)


def test_workday_keys_on_tenant_and_req():
    a = "https://cigna.wd5.myworkdayjobs.com/cignacareers/job/Austin-TX/AI-Engineer_R123"
    b = "https://cigna.wd5.myworkdayjobs.com/External/job/Remote/AI-Engineer_R123?source=x"
    c = "https://cigna.wd5.myworkdayjobs.com/en-US/cignacareers/job/AI-Engineer_R123"
    assert identity.posting_key(a) == identity.posting_key(b) == identity.posting_key(c) == "workday|cigna|ai-engineer_r123"


def test_rippling_keys_on_its_posting_id_with_or_without_a_locale():
    a = "https://ats.rippling.com/vouch-inc/jobs/a7099d11-3586-4b85-9d0c-904c9203c8d0"
    b = "https://ats.rippling.com/en-US/vouch-inc/jobs/a7099d11-3586-4b85-9d0c-904c9203c8d0?jobSite=Indeed"
    assert identity.posting_key(a) == identity.posting_key(b) == "rippling|a7099d11-3586-4b85-9d0c-904c9203c8d0"


def test_rippling_is_recognised_but_never_a_board_lane():
    assert "rippling" not in ATS_SOURCE_NAMES
    url = "https://ats.rippling.com/vouch-inc/jobs/a7099d11-3586-4b85-9d0c-904c9203c8d0"
    assert is_applyable(url) and board_slug(url) == "vouch-inc"


def test_blank_url_has_no_key():
    assert identity.posting_key("") == "" and identity.posting_key(None) == ""


def test_company_from_url():
    assert identity.company_from_url("https://job-boards.greenhouse.io/edison-scientific/jobs/1") \
        == "Edison Scientific"
    assert identity.company_from_url("https://www.linkedin.com/jobs/view/1") == ""
    assert identity.company_from_url("https://job-boards.greenhouse.io/nc/jobs/1") == ""


@pytest.mark.parametrize("name", ["siehe Beschreibung", "Non renseigné", "  NON RENSEIGNE "])
def test_a_placeholder_company_is_no_company(name):
    assert identity.normalize_company(name) == ""


@pytest.mark.parametrize("url, key", [
    ("https://jpmc.fa.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=CX_1001#210632946",
     "oracle|jpmc|210632946"),
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/12345",
     "oracle|eeho|12345"),
])
def test_oracle_postings_key_on_tenant_and_requisition(url, key):
    assert identity.posting_key(url) == key
    assert identity.posting_key(url.replace("210632946", "1").replace("12345", "1")) != key
