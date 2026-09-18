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
from urllib.parse import parse_qs, urlparse

from src.apply.domscan import FormScan, scan_form
from src.apply.reconcile import Reconciled, reconcile
from src.apply.schema import BoardSchema, fetch_questions
from src.ats_http import CareersError, fetch_text
from src.discovery.sources.ats import registry as ats_registry

EMBED_URL = "https://boards.greenhouse.io/embed/job_app?token={token}"

# Shared board table: host variants (.eu included) and posting shape live in
# one place. The slug is captured for cross-checking only.
_GH = ats_registry.get_source("greenhouse")
_PATH_URL = _GH.posting_url_re
# The slug as it appears in the rendered form's action. `&` arrives escaped.
_FORM_ACTION = re.compile(
    r'<form[^>]*\baction="/embed/job_app\?for=(?P<slug>[^&"]+)&(?:amp;)?token=(?P<token>\d+)"',
    re.IGNORECASE,
)
_TOKEN = re.compile(r"^\d+$")


class ApplyUrlError(Exception):
    """The posting URL carries no Greenhouse job token."""


class PostingExpired(Exception):
    """The board no longer serves this posting. An ordinary outcome, not a
    failure of the run."""


@dataclass(frozen=True)
class Posting:
    token: str
    url_slug: str | None    # from the URL, when it had one; cross-check only

    @property
    def form_url(self) -> str:
        return EMBED_URL.format(token=self.token)


@dataclass(frozen=True)
class BoardForm:
    posting: Posting
    slug: str               # recovered from the rendered form
    html: str
    scan: FormScan
    schema: BoardSchema
    reconciled: Reconciled


def _query_values(query: dict[str, list[str]], keys: tuple[str, ...]) -> set[str]:
    """Non-empty values under any of `keys`, deduped across all of them."""
    return {v.strip() for k in keys for v in query.get(k, []) if v.strip()}


def parse_posting(url: str) -> Posting:
    """Pull the job token out of a posting URL.

    Three shapes, all live in clean.parquet: the board path form, a company
    careers page carrying `?gh_jid=`, and the embed form `?for=<slug>&token=<id>`.
    The query-param spellings come from the shared registry rather than being
    named here, so detection and parsing cannot drift. The slug is read where
    present, but nothing depends on it.
    """
    text = (url or "").strip()
    if not text:
        raise ApplyUrlError("no URL")

    match = _PATH_URL.match(text)
    if match:
        return Posting(token=match.group("posting_id"), url_slug=match.group("slug"))

    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    # Duplicated params are real: one live row spells ?gh_jid=X&gh_jid=X.
    tokens = _query_values(query, _GH.id_query_keys)
    if not tokens:
        if _GH.matches_host((parsed.hostname or "").lower()):
            raise ApplyUrlError(f"Greenhouse URL with no job token: {text}")
        raise ApplyUrlError(f"not a Greenhouse posting URL: {text}")
    if len(tokens) > 1:
        raise ApplyUrlError(f"URL carries {len(tokens)} different job tokens: {text}")
    token = tokens.pop()
    if not _TOKEN.match(token):
        raise ApplyUrlError(f"job token is not numeric ({token!r}): {text}")

    boards = _query_values(query, _GH.slug_query_keys)
    return Posting(token=token, url_slug=boards.pop() if len(boards) == 1 else None)


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
        raise CareersError(
            f"form is for token {match.group('token')}, not {token}"
        )
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
        # Never seen live (0 of 22 sampled). The rendered form wins — it is what
        # the token actually resolves to — but a silent swap is worth a shout.
        raise CareersError(
            f"URL names board {posting.url_slug!r} but token {posting.token} "
            f"resolves to {slug!r}"
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
