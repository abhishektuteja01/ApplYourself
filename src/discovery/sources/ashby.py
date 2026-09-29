"""Ashby: the posting API when the board has it on, else the board page's own
GraphQL, which lists without JDs."""
from __future__ import annotations

from urllib.parse import quote

from src.ats_http import CareersError, fetch_json, fetch_json_post
from src.discovery import normalize as N
from src.discovery.sources.boards import Board, BoardLane, job_items
from src.discovery.store import make_posting

API = "https://api.ashbyhq.com/posting-api/job-board"
GRAPHQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"
BOARD_QUERY = (
    "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) "
    "{ jobBoard: jobBoardWithTeams("
    "organizationHostedJobsPageName: $organizationHostedJobsPageName) "
    "{ jobPostings { id title locationName workplaceType employmentType } } }")
POSTING_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, "
    "$jobPostingId: String!) { jobPosting("
    "organizationHostedJobsPageName: $organizationHostedJobsPageName, "
    "jobPostingId: $jobPostingId) { id descriptionHtml publishedDate } }")


def _salary(comp) -> tuple:
    if not isinstance(comp, dict):
        return None, None, ""
    for tier in comp.get("compensationTiers") or []:
        for part in (tier or {}).get("components") or [] if isinstance(tier, dict) else []:
            if isinstance(part, dict) and part.get("compensationType") == "Salary":
                return part.get("minValue"), part.get("maxValue"), part.get("currencyCode") or ""
    return None, None, ""


def posting_from(item: dict, company: str) -> dict | None:
    title = item.get("title") or ""
    url = item.get("jobUrl") or item.get("applyUrl") or ""
    if not title or not url:
        return None
    lo, hi, currency = _salary(item.get("compensation"))
    return make_posting(
        source="ashby", company=company, title=title, url=url,
        jd_text=N.html_to_text(item.get("descriptionHtml") or item.get("descriptionPlain")),
        location_raw=item.get("location") or "",
        posted_date=N.iso_date(item.get("publishedAt")),
        remote_flag=item.get("isRemote") is True,
        salary_min=lo, salary_max=hi, salary_currency=currency,
        employment_type=item.get("employmentType") or "",
        _id=item.get("id"),
    )


def list_rest(slug: str, company: str, deadline_ts=None) -> list[dict]:
    payload = fetch_json(f"{API}/{slug}?includeCompensation=true", deadline_ts=deadline_ts)
    return [p for p in (posting_from(i, company) for i in job_items(payload, "jobs")) if p]


def graphql(op: str, query: str, variables: dict, deadline_ts: float | None = None) -> dict:
    """The `data` of one GraphQL call. A rejected query is transient: it is
    not evidence the board is gone."""
    payload = fetch_json_post(f"{GRAPHQL_URL}?op={op}",
                              {"operationName": op, "variables": variables, "query": query},
                              deadline_ts=deadline_ts)
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    if payload.get("errors"):
        raise CareersError(f"ashby graphql {op} errors: {payload['errors'][:1]}")
    return payload.get("data") or {}


def graphql_board(slug: str, deadline_ts: float | None = None) -> list[dict] | None:
    """The board's postings, or None when Ashby has no board under `slug`."""
    board = graphql("ApiJobBoardWithTeams", BOARD_QUERY,
                    {"organizationHostedJobsPageName": slug}, deadline_ts).get("jobBoard")
    return None if board is None else job_items(board, "jobPostings")


class AshbyLane(BoardLane):
    name = "ashby"

    def list_board(self, board: Board, crawl, pause) -> list[dict]:
        try:
            return list_rest(board.slug, board.name, crawl.deadline_ts)
        except CareersError as e:
            if e.status != 404:
                raise
            not_found = e
        # The posting API answers 404 for a live board whose org has not
        # enabled it. `jobBoard: null` is the only answer that means gone.
        pause()
        items = graphql_board(board.slug, crawl.deadline_ts)
        if items is None:
            raise not_found
        out = []
        for item in items:
            if not item.get("id") or not item.get("title"):
                continue
            out.append(make_posting(
                source="ashby", company=board.name, title=item["title"],
                url=f"https://jobs.ashbyhq.com/{quote(board.slug)}/{item['id']}",
                location_raw=item.get("locationName") or "",
                remote_flag=item.get("workplaceType") == "Remote",
                employment_type=item.get("employmentType") or "",
                _id=item["id"],
            ))
        return out

    def detail(self, board: Board, posting: dict, crawl) -> dict | None:
        found = graphql("ApiJobPosting", POSTING_QUERY,
                        {"organizationHostedJobsPageName": board.slug,
                         "jobPostingId": posting["_id"]}, crawl.deadline_ts).get("jobPosting")
        if not isinstance(found, dict):
            return None
        return {**posting, "jd_text": N.html_to_text(found.get("descriptionHtml")),
                "posted_date": N.iso_date(found.get("publishedDate"))}
