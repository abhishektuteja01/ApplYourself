"""Get from a scraped posting URL to a reconciled application form.

The token is the only thing the URL has to give up. Measured over 58 live
postings, `embed/job_app?token=<token>` renders the form with no `for=<slug>`
at all, and the rendered form's own action carries the slug back:

    <form action="/embed/job_app?for=asteralabs&token=4719162005" ...>

That matters because a third of the Greenhouse rows in clean.parquet are company
careers pages carrying only `?gh_jid=`, with the slug nowhere in the URL. Fetch
by token, read the slug off the form, then use it for the question API — which
does need the slug. A guessed slug fails safe either way: the embed URL 404s on
one that does not own the token.

Network lives here; the parsing either side of it is pure and tested offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from src.apply.domscan import FormScan, scan_form
from src.apply.reconcile import Reconciled, reconcile
from src.apply.schema import BoardSchema, fetch_questions
from src.shared import job_boards
from src.shared.http_client import CareersError, fetch_text

EMBED_URL = "https://boards.greenhouse.io/embed/job_app?token={token}"

# Shared board table: host variants (.eu included), posting shape and query
# keys live in one place. The slug is captured for cross-checking only.
_GH = job_boards.get_source("greenhouse")
# The slug as it appears in the rendered form's action. `&` arrives escaped.
_FORM_ACTION = re.compile(
    r'<form[^>]*\baction="/embed/job_app\?for=(?P<slug>[^&"]+)&(?:amp;)?token=(?P<token>\d+)"',
    re.IGNORECASE,
)


class ApplyUrlError(Exception):
    """The posting URL carries no Greenhouse job token."""


class PostingExpired(Exception):
    """The board no longer serves this posting. An ordinary outcome, not a
    failure of the run."""


@dataclass(frozen=True)
class Posting:
    token: str
    url_slug: str | None  # from the URL, when it had one; cross-check only

    @property
    def form_url(self) -> str:
        return EMBED_URL.format(token=self.token)


@dataclass(frozen=True)
class BoardForm:
    posting: Posting
    slug: str  # recovered from the rendered form
    html: str
    scan: FormScan
    schema: BoardSchema
    reconciled: Reconciled


def parse_posting(url: str) -> Posting:
    """Pull the job token out of a posting URL.

    Three shapes, all live in clean.parquet: the board path form, a company
    careers page carrying `?gh_jid=`, and the embed form `?for=<slug>&token=<id>`.
    Parsing is the shared registry's, so detection and parsing cannot drift;
    this only adds the reason when it recognizes nothing. The slug is read
    where present, but nothing depends on it.
    """
    text = (url or "").strip()
    if not text:
        raise ApplyUrlError("no URL")

    hit = job_boards.parse_posting_url(text)
    if hit is not None and hit.source == _GH.name:
        return Posting(token=hit.posting_id, url_slug=hit.slug or None)

    tokens, _ = job_boards.query_values(_GH, text)
    if len(tokens) > 1:
        raise ApplyUrlError(f"URL carries {len(tokens)} different job tokens: {text}")
    token = next(iter(tokens), "")
    if token and not token.isdigit():
        raise ApplyUrlError(f"job token is not numeric ({token!r}): {text}")
    if _GH.matches_host((urlparse(text).hostname or "").lower()):
        raise ApplyUrlError(f"Greenhouse URL with no job token: {text}")
    raise ApplyUrlError(f"not a Greenhouse posting URL: {text}")


def slug_from_form(html: str, token: str) -> str:
    """The board slug, read off the rendered form's own action.

    Raises when the form names a different token — that would mean the response
    is some other posting's form, and every field in it belongs to another role.
    """
    match = _FORM_ACTION.search(html or "")
    if match is None:
        raise CareersError(
            "rendered form carries no /embed/job_app action to read the board "
            "slug from — the embed page shape has changed"
        )
    if match.group("token") != str(token):
        raise CareersError(f"form is for token {match.group('token')}, not {token}")
    return match.group("slug")


def fetch_form(posting: Posting, timeout: int = 30) -> tuple[str, str]:
    """(html, slug) for a posting. Raises PostingExpired on 404."""
    try:
        html = fetch_text(posting.form_url, timeout=timeout)
    except CareersError as exc:
        if exc.status == 404:
            raise PostingExpired(
                f"posting {posting.token} is gone (404): {posting.form_url}"
            ) from exc
        raise
    return html, slug_from_form(html, posting.token)


def load_board(url: str, timeout: int = 30) -> BoardForm:
    """Posting URL -> reconciled form. Two GETs: the form, then its schema."""
    posting = parse_posting(url)
    html, slug = fetch_form(posting, timeout=timeout)
    if posting.url_slug and posting.url_slug.casefold() != slug.casefold():
        # Never seen live (0 of 22 sampled). Refused rather than trusting
        # either side: one of them names another board's posting.
        raise CareersError(
            f"URL names board {posting.url_slug!r} but token {posting.token} resolves to {slug!r}"
        )
    scan = scan_form(html)
    schema = fetch_questions(slug, posting.token, timeout=timeout)
    return BoardForm(
        posting=posting,
        slug=slug,
        html=html,
        scan=scan,
        schema=schema,
        reconciled=reconcile(scan, schema),
    )
