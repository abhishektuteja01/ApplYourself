from __future__ import annotations

import time
from urllib.parse import quote

from src.ats_http import CareersError, fetch_json_post
from src.discovery import cleaning, gate
from src.discovery.sources.ats.base import AtsBoardSource, PAYLOAD_SHAPE_ERRORS, job_items
from src.discovery.dates import iso_date
from src.discovery.htmlutil import html_to_text
from src.discovery.schema import make_row

def _ashby_salary(comp) -> tuple:
    if not isinstance(comp, dict):
        return None, None, ""
    for tier in comp.get("compensationTiers") or []:
        if not isinstance(tier, dict):
            continue
        for component in tier.get("components") or []:
            if isinstance(component, dict) and component.get("compensationType") == "Salary":
                return (
                    component.get("minValue"),
                    component.get("maxValue"),
                    component.get("currencyCode") or "",
                )
    return None, None, ""

# The posting API is opt-in per organization and answers 404 for a live board
# whose org has not enabled it. The board page's own GraphQL calls are the
# fallback: `jobBoard: null` is the only answer that means the board is gone.
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


def graphql(op: str, query: str, variables: dict, deadline_ts: float | None = None) -> dict:
    """The `data` object of one GraphQL call. A rejected query raises a
    transient `CareersError`: it is not evidence the board is gone."""
    payload = fetch_json_post(f"{GRAPHQL_URL}?op={op}",
                              {"operationName": op, "variables": variables, "query": query},
                              deadline_ts=deadline_ts)
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    if payload.get("errors"):
        raise CareersError(f"ashby graphql {op} errors: {payload['errors'][:1]}")
    return payload.get("data") or {}


def graphql_board(slug: str, deadline_ts: float | None = None) -> list[dict] | None:
    """The board's postings, or None if Ashby has no board under `slug`."""
    board = graphql("ApiJobBoardWithTeams", BOARD_QUERY,
                    {"organizationHostedJobsPageName": slug}, deadline_ts).get("jobBoard")
    if board is None:
        return None
    return job_items(board, "jobPostings")


def ashby_rows(payload, company: str) -> list[dict]:
    rows: list[dict] = []
    for item in job_items(payload, "jobs"):
        title = item.get("title") or ""
        job_url = item.get("jobUrl") or item.get("applyUrl") or ""
        if not title or not job_url:
            continue

        salary_min, salary_max, currency = _ashby_salary(item.get("compensation"))

        rows.append(make_row(
            site="ashby",
            company=company,
            title=title,
            job_url=job_url,
            description=html_to_text(
                item.get("descriptionHtml") or item.get("descriptionPlain")
            ),
            location=item.get("location") or "",
            date_posted=iso_date(item.get("publishedAt")),
            is_remote=item.get("isRemote") is True,
            min_amount=salary_min,
            max_amount=salary_max,
            currency=currency,
            job_type=item.get("employmentType") or "",
        ))
    return rows

class AshbySource(AtsBoardSource):
    name = "ashby"

    def board_url(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

    def parse_rows(self, payload, company: str) -> list[dict]:
        return ashby_rows(payload, company)

    def fetch_board(self, c, ctx, pacing: float) -> list[dict]:
        try:
            return super().fetch_board(c, ctx, pacing)
        except CareersError as e:
            if e.status != 404:
                raise
            not_found = e
        time.sleep(pacing)
        postings = graphql_board(c.slug, ctx.deadline_ts)
        if postings is None:
            raise not_found
        return self._graphql_rows(c, ctx, pacing, postings)

    def _graphql_rows(self, c, ctx, pacing: float, postings: list[dict]) -> list[dict]:
        """Rows for a board only GraphQL serves. The list carries no
        description or date, so each posting that passes the title gate costs
        one paced detail call; the rest cost nothing."""
        candidates = []
        for item in postings:
            title = item.get("title") or ""
            vertical = cleaning.classify_vertical_from_title(title) if title else None
            if item.get("id") and vertical:
                candidates.append((item, vertical))
        verdicts = gate.passing_titles(
            [(item["title"], v) for item, v in candidates], ctx.verticals, self.name)

        rows: list[dict] = []
        for (item, _), ok in zip(candidates, verdicts):
            if not ok:
                continue
            if ctx.deadline_reached():
                break
            time.sleep(pacing)
            try:
                detail = graphql("ApiJobPosting", POSTING_QUERY,
                                 {"organizationHostedJobsPageName": c.slug,
                                  "jobPostingId": item["id"]},
                                 ctx.deadline_ts).get("jobPosting")
            except (CareersError, *PAYLOAD_SHAPE_ERRORS):
                # One unreadable posting must not cost the board its other rows.
                continue
            if not isinstance(detail, dict):
                continue
            rows.append(make_row(
                site="ashby",
                company=c.name,
                title=item["title"],
                job_url=f"https://jobs.ashbyhq.com/{quote(c.slug)}/{item['id']}",
                description=html_to_text(detail.get("descriptionHtml")),
                location=item.get("locationName") or "",
                date_posted=iso_date(detail.get("publishedDate")),
                is_remote=item.get("workplaceType") == "Remote",
                job_type=item.get("employmentType") or "",
            ))
        return rows
