"""Retrying HTTP fetchers shared by two consumers: discovery's ATS board
scrapers and `src/apply/`'s live application-form fetching. The retry and
timeout constants below govern both — a change here reaches live forms.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
REQUEST_TIMEOUT = 30
# Retry-After is remote input; a board asking for an hour gets skipped instead.
MAX_RETRY_AFTER = 60
_HEADERS = {"User-Agent": "job-search-pipeline (personal job-search pipeline)"}

# One observer per thread: discovery runs its lanes on separate threads, so a
# lane that traces sees only its own requests. Unset, nothing is recorded.
_local = threading.local()


@contextmanager
def observe(fn):
    """Call `fn(event)` after every request attempt made on this thread."""
    prev = getattr(_local, "observer", None)
    _local.observer = fn
    try:
        yield
    finally:
        _local.observer = prev


def _notify(**event) -> None:
    fn = getattr(_local, "observer", None)
    if fn is None:
        return
    try:
        fn(event)
    except Exception:  # noqa: BLE001 -- a broken trace must never fail a fetch
        log.warning("ats_http observer failed", exc_info=True)


class CareersError(Exception):
    """Board fetch failed after retries, or the board does not exist.

    `permanent` is the only thing the health ledger may act on: a permanent
    failure counts toward pruning, a transient one never does.
    """

    def __init__(self, message: str, status: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent

def _sleep_or_raise(delay: float, deadline_ts: float | None, url: str) -> None:
    """Sleep `delay`, unless that would cross the run deadline."""
    if deadline_ts and time.time() + delay > deadline_ts:
        raise CareersError(f"deadline reached before retry ({delay:.1f}s): {url}")
    time.sleep(delay)

def _fetch(
    url: str,
    read,
    timeout: int = REQUEST_TIMEOUT,
    deadline_ts: float | None = None,
    json_body: dict | None = None,
):
    """GET (or, with `json_body`, POST) url, retrying 429/5xx, and hand the 200
    response to `read`.

    `read` owns what a 200 body means: a body it cannot use is a wall or a wrong
    slug rather than a blip, so it raises instead of retrying.
    """
    method = "POST" if json_body is not None else "GET"
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            resp = (
                requests.post(url, json=json_body, timeout=timeout, headers=_HEADERS)
                if json_body is not None
                else requests.get(url, timeout=timeout, headers=_HEADERS)
            )
        except requests.RequestException as exc:
            _notify(method=method, url=url, attempt=attempt, status=None,
                    secs=time.monotonic() - t0, error=type(exc).__name__)
            if attempt == MAX_RETRIES:
                raise CareersError(f"fetch failed: {url}: {exc}") from exc
            _sleep_or_raise(RETRY_BASE_DELAY * attempt, deadline_ts, url)
            continue
        if getattr(_local, "observer", None) is not None:
            _notify(method=method, url=url, attempt=attempt, status=resp.status_code,
                    secs=time.monotonic() - t0,
                    retry_after=resp.headers.get("Retry-After"),
                    cf_mitigated=resp.headers.get("cf-mitigated"))
        if resp.status_code == 200:
            return read(resp)
        if resp.status_code == 404:
            raise CareersError(
                f"board not found (404) — check the slug: {url}",
                status=404,
                permanent=True,
            )
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt == MAX_RETRIES:
                raise CareersError(
                    f"HTTP {resp.status_code} after {MAX_RETRIES} attempts: {url}",
                    status=resp.status_code,
                )
            retry_after = resp.headers.get("Retry-After")
            delay = (
                min(float(retry_after), MAX_RETRY_AFTER)
                if retry_after and retry_after.isdigit()
                else RETRY_BASE_DELAY * (2 ** attempt)
            )
            _sleep_or_raise(delay, deadline_ts, url)
            continue
        # 401/403 walls included: a live board behind Cloudflare must not be pruned.
        raise CareersError(f"HTTP {resp.status_code}: {url}", status=resp.status_code)
    # Unreachable: at attempt == MAX_RETRIES every branch above raises or
    # returns. Kept as a guard.
    raise CareersError(f"exhausted retries: {url}")  # pragma: no cover


def _json_reader(url: str):
    """A `read` for `_fetch` that decodes JSON. A 200 that is not JSON is a wall
    or a wrong slug, not a blip, so it is permanent."""
    def read(resp):
        try:
            return resp.json()
        except ValueError as exc:
            raise CareersError(
                f"invalid JSON body: {url}: {exc}", status=200, permanent=True
            ) from exc

    return read


def fetch_json(
    url: str,
    timeout: int = REQUEST_TIMEOUT,
    deadline_ts: float | None = None,
):
    """GET url -> parsed JSON. Retries 429/5xx (Retry-After capped at MAX_RETRY_AFTER)."""
    return _fetch(url, _json_reader(url), timeout=timeout, deadline_ts=deadline_ts)


def fetch_json_post(
    url: str,
    json_body: dict,
    timeout: int = REQUEST_TIMEOUT,
    deadline_ts: float | None = None,
):
    """POST json_body to url -> parsed JSON, same retry policy as fetch_json.

    Workday's list/search endpoint takes its query as a POST body rather than
    query parameters — everything else about the response (200/404/429/5xx)
    behaves the same as every other board API here.
    """
    return _fetch(
        url, _json_reader(url), timeout=timeout, deadline_ts=deadline_ts, json_body=json_body
    )


def fetch_text(
    url: str,
    timeout: int = REQUEST_TIMEOUT,
    deadline_ts: float | None = None,
) -> str:
    """GET url -> response body as text, same retry policy as fetch_json.

    For the rendered application form, which is server-rendered HTML rather than
    JSON. An empty 200 body is permanent: a board that answers with nothing is
    not going to answer with something on a retry.
    """
    def read(resp) -> str:
        text = resp.text or ""
        if not text.strip():
            raise CareersError(f"empty body: {url}", status=200, permanent=True)
        return text

    return _fetch(url, read, timeout=timeout, deadline_ts=deadline_ts)
