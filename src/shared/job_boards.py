"""One table per ATS board: which hosts are it, how its posting URLs are
shaped, and whether `/apply` can submit to it.

Single source of truth for board identity. Discovery (posting keys, one row per job, ingest) and
submission (`src/apply/`) both read it, so the shortlist and the queue cannot
disagree about what a URL is.

Deterministic and LLM-free (R7). Vendor names are structurally required.

Not here on purpose: `drivers.py`'s SUBMIT_REQUEST_* tables. Those are measured
submit-*endpoint* paths, not board identity — folding them in would break the
POST-gating that makes submission detection safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class AtsSource:
    name: str
    #: Matched as `(?:^|\.)<suffix>$` against the hostname — never as a
    #: substring of the whole URL, which `evilgreenhouse.io.example.com`
    #: would satisfy.
    host_suffixes: tuple[str, ...]
    #: Full-URL prefix match. Named groups: slug, posting_id, and
    #: region/pod/site_id where the board has them.
    posting_url_re: re.Pattern[str]
    #: Query-param fallbacks for a board whose posting can be identified off
    #: an arbitrary careers-page host (Greenhouse's `?gh_jid=`).
    slug_query_keys: tuple[str, ...] = ()
    id_query_keys: tuple[str, ...] = ()
    #: Whether `/apply` can submit without a human. Workday is False: it is
    #: discovered and scored like any other board but is manual-apply only,
    #: and has no browser driver.
    submittable: bool = False
    driver_name: str | None = None
    #: Whether discovery crawls it as a board lane. False: its urls are only
    #: recognised (posting key, board, applyable).
    board_lane: bool = True
    host_res: tuple[re.Pattern[str], ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "host_res",
            tuple(
                re.compile(rf"(?:^|\.){re.escape(s)}$", re.IGNORECASE) for s in self.host_suffixes
            ),
        )

    def matches_host(self, host: str) -> bool:
        return any(r.search(host) for r in self.host_res)


_UUIDISH = r"[0-9a-fA-F][0-9a-fA-F-]{7,}"

ATS_SOURCES: tuple[AtsSource, ...] = (
    AtsSource(
        name="greenhouse",
        host_suffixes=("greenhouse.io",),
        posting_url_re=re.compile(
            r"^https?://(?:job-boards|boards)\.(?:(?P<region>[a-z]{2,4})\.)?greenhouse\.io/"
            r"(?:embed/job_app/)?(?P<slug>[^/?#]+)/jobs/(?P<posting_id>\d+)(?:[/?#]|$)",
            re.IGNORECASE,
        ),
        slug_query_keys=("for", "board"),
        id_query_keys=("token", "gh_jid"),
        submittable=True,
        driver_name="BrowserDriver",
    ),
    AtsSource(
        name="lever",
        host_suffixes=("lever.co",),
        posting_url_re=re.compile(
            r"^https?://jobs\.(?:(?P<region>eu)\.)?lever\.co/"
            rf"(?P<slug>[^/?#]+)/(?P<posting_id>{_UUIDISH})",
            re.IGNORECASE,
        ),
        submittable=True,
        driver_name="LeverBrowserDriver",
    ),
    AtsSource(
        name="ashby",
        host_suffixes=("ashbyhq.com",),
        posting_url_re=re.compile(
            r"^https?://jobs\.ashbyhq\.com/"
            rf"(?P<slug>[^/?#]+)/(?P<posting_id>{_UUIDISH})",
            re.IGNORECASE,
        ),
        submittable=True,
        driver_name="AshbyBrowserDriver",
    ),
    AtsSource(
        name="workday",
        host_suffixes=("myworkdayjobs.com",),
        # The tenant is the first host label, not a path segment.
        posting_url_re=re.compile(
            r"^https?://(?P<slug>[^./]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com"
            r"(?:/wday/cxs/[^/?#]+)?"
            r"(?:/(?P<site_id>[^/?#]+))?"
            r"(?:/job/[^?#]*?/(?P<posting_id>[^/?#]+))?",
            re.IGNORECASE,
        ),
    ),
    AtsSource(
        name="eightfold",
        # Custom careers hosts (a company's own domain) are not recognised;
        # their postings key on host + path like any careers page.
        host_suffixes=("eightfold.ai",),
        posting_url_re=re.compile(
            r"^https?://(?P<slug>[^./]+)\.eightfold\.ai/careers/job/(?P<posting_id>\d+)",
            re.IGNORECASE,
        ),
    ),
    AtsSource(
        name="oracle",
        host_suffixes=("oraclecloud.com",),
        # Oracle Recruiting Cloud: the requisition id rides in the fragment of
        # the search-page form, or in the path of the job-page form.
        posting_url_re=re.compile(
            r"^https?://(?P<slug>[^./]+)\.fa\.(?:[\w-]+\.)*oraclecloud\.com/"
            r"(?:hcmUI/CandidateExperience/[^/]+/sites/(?P<site_id>[^/?#]+)/job/|\?[^#]*#)"
            r"(?P<posting_id>[\w-]+)",
            re.IGNORECASE,
        ),
        board_lane=False,
    ),
    AtsSource(
        name="rippling",
        host_suffixes=("ats.rippling.com",),
        posting_url_re=re.compile(
            r"^https?://ats\.rippling\.com/(?:[a-z]{2}-[a-z]{2}/)?"
            rf"(?P<slug>[^/?#]+)/jobs/(?P<posting_id>{_UUIDISH})",
            re.IGNORECASE,
        ),
        board_lane=False,
    ),
)

_BY_NAME = {s.name: s for s in ATS_SOURCES}

# Tuple, not a set: iterated to build ordered output.
ATS_SOURCE_NAMES: tuple[str, ...] = tuple(s.name for s in ATS_SOURCES if s.board_lane)

#: Boards `apply run --submit` can actually reach.
SUBMITTABLE_SOURCES: frozenset[str] = frozenset(s.name for s in ATS_SOURCES if s.submittable)

#: Board -> browser driver class name. Keys equal SUBMITTABLE_SOURCES by
#: construction, so a driver cannot be added without widening the promise the
#: shortlist makes.
DRIVER_NAMES: dict[str, str] = {s.name: s.driver_name for s in ATS_SOURCES if s.driver_name}


@dataclass(frozen=True)
class PostingUrl:
    source: str
    slug: str = ""
    posting_id: str = ""
    region: str = ""
    pod: str = ""
    site_id: str = ""


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def query_values(src: AtsSource, url: str) -> tuple[set[str], set[str]]:
    """(ids, slugs): the distinct non-empty values under the board's id and
    slug query keys. Duplicated params are real (one live row spells
    ?gh_jid=X&gh_jid=X), so values are deduped across keys."""
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return set(), set()

    def values(keys: tuple[str, ...]) -> set[str]:
        return {v.strip() for k in keys for v in query.get(k, []) if v.strip()}

    return values(src.id_query_keys), values(src.slug_query_keys)


def _query_fallback(src: AtsSource, url: str) -> PostingUrl | None:
    """Identify a posting off query params alone — a company careers page
    carrying `?gh_jid=`, or Greenhouse's own `?for=&token=` embed form."""
    if not src.id_query_keys:
        return None
    ids, slugs = query_values(src, url)
    # Two *different* ids are not resolvable and stay unrecognized.
    if len(ids) != 1:
        return None
    posting_id = ids.pop()
    if not posting_id.isdigit():
        return None
    return PostingUrl(
        source=src.name, slug=slugs.pop() if len(slugs) == 1 else "", posting_id=posting_id
    )


def parse_posting_url(url: str) -> PostingUrl | None:
    """The board, slug and posting id a URL names, or None if it names none."""
    text = (url or "").strip()
    if not text:
        return None
    host = _host(text)
    for src in ATS_SOURCES:
        if src.matches_host(host):
            m = src.posting_url_re.match(text)
            if m:
                g = m.groupdict()
                return PostingUrl(
                    source=src.name,
                    slug=g.get("slug") or "",
                    posting_id=g.get("posting_id") or "",
                    region=(g.get("region") or "").lower(),
                    pod=(g.get("pod") or "").lower(),
                    site_id=g.get("site_id") or "",
                )
            hit = _query_fallback(src, text)
            if hit:
                return hit
            return None
    # An arbitrary careers host can still carry a board's posting id.
    for src in ATS_SOURCES:
        hit = _query_fallback(src, text)
        if hit:
            return hit
    return None


def detect_source(url: str) -> str | None:
    """The board name this URL belongs to, or None. Always the plain source
    name — region never rides along."""
    hit = parse_posting_url(url)
    return hit.source if hit else None


def board_slug(url: str) -> str:
    """The board's own tenant identifier, or "" when the URL names no board.
    Workday's is the tenant subdomain, not a path segment."""
    hit = parse_posting_url(url)
    return hit.slug if hit else ""


def key_of(ats: str, slug: str) -> str:
    """A board's key, `ats|slug` with the slug lowercased: one board per
    (ats, slug) whatever the case."""
    return f"{ats}|{slug.lower()}"


def board_key(url: str, *, tenant_only: bool = False) -> str:
    """The key of the board a posting url belongs to, or "". A Workday key
    carries the pod and site, as its boards.csv slug does; `tenant_only` gives
    the store's `board` (`workday|tenant`)."""
    hit = parse_posting_url(url)
    if hit is None or not hit.slug:
        return ""
    if hit.source == "workday" and not tenant_only:
        if not (hit.pod and hit.site_id):
            return ""
        return key_of(hit.source, f"{hit.slug}|{hit.pod}|{hit.site_id}")
    return key_of(hit.source, hit.slug)


def is_applyable(url: str) -> bool:
    """Whether this url leads to a board's own application form.

    Broader than `detect_source`: a board host with no posting id on it is
    still the board, and still beats an aggregator repost when clean.parquet
    picks one posting per job_id. Narrower
    than a substring test: the match is on the hostname, so
    `evilgreenhouse.io.example.com` is not Greenhouse.
    """
    text = (url or "").strip()
    if not text:
        return False
    host = _host(text)
    if any(src.matches_host(host) for src in ATS_SOURCES):
        return True
    return parse_posting_url(text) is not None


def get_source(name: str) -> AtsSource | None:
    return _BY_NAME.get(name)


def source(name: str) -> AtsSource:
    """A board the code drives by name; KeyError when it is not registered."""
    return _BY_NAME[name]
