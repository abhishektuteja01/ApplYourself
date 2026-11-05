"""Retrying HTTP fetchers shared by two consumers: discovery's ATS board
scrapers and `src/apply/`'s live application-form fetching. The retry and
timeout constants below govern both — a change here reaches live forms.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
REQUEST_TIMEOUT = 30
# Retry-After is remote input: honoured, but never for longer than this.
MAX_RETRY_AFTER = 60
_HEADERS = {"User-Agent": "job-search-pipeline (personal job-search pipeline)"}

# One observer per thread: discovery runs its lanes on separate threads, so a
# lane that traces sees only its own requests. Unset, nothing is recorded.
_local = threading.local()


def _session() -> requests.Session:
    """This thread's session, so a lane reuses its TLS connections. Cookies are
    refused, which keeps every request as stateless as a bare `requests.get`."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        _local.session = session
    return session


def _get(url, **kwargs):
    return _session().get(url, **kwargs)


def _post(url, **kwargs):
    return _session().post(url, **kwargs)


class HttpTally:
    """Thread-safe request counts per host: requests, seconds spent waiting on
    the network, 403s, 429s and transport errors. Feed it with `observe`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.hosts: dict[str, dict] = {}

    def record(self, url: str, secs: float, status: int | None) -> None:
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            h = self.hosts.setdefault(
                host, {"requests": 0, "seconds": 0.0, "403": 0, "429": 0, "errors": 0}
            )
            h["requests"] += 1
            h["seconds"] += secs
            if status is None:
                h["errors"] += 1
            elif status in (403, 429):
                h[str(status)] += 1

    def on_http(self, event: dict) -> None:
        self.record(event.get("url", ""), event.get("secs", 0.0), event.get("status"))

    def totals(self) -> dict:
        with self._lock:
            out = {"requests": 0, "seconds": 0.0, "403": 0, "429": 0, "errors": 0}
            for h in self.hosts.values():
                for k in out:
                    out[k] += h[k]
            out["seconds"] = round(out["seconds"], 1)
            return out


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
    except Exception:  # a broken trace must never fail a fetch
        log.warning("http_client observer failed", exc_info=True)


class CareersError(Exception):
    """Board fetch failed after retries, or the board does not exist.

    `permanent` decides what the board ledger records: a permanent 404 is a
    strike toward `dead`; a transient failure leaves `last_checked` alone.
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
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            resp = (
                _post(url, json=json_body, timeout=timeout, headers=_HEADERS)
                if json_body is not None
                else _get(url, timeout=timeout, headers=_HEADERS)
            )
        except requests.RequestException as exc:
            _notify(url=url, secs=time.monotonic() - t0, status=None)
            if attempt == MAX_RETRIES:
                raise CareersError(f"fetch failed: {url}: {exc}") from exc
            _sleep_or_raise(RETRY_BASE_DELAY * attempt, deadline_ts, url)
            continue
        _notify(url=url, secs=time.monotonic() - t0, status=resp.status_code)
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
            delay = _retry_after(resp.headers.get("Retry-After"))
            if delay is None:
                delay = RETRY_BASE_DELAY * (2**attempt)
            _sleep_or_raise(delay, deadline_ts, url)
            continue
        # 401/403 walls included: a live board behind Cloudflare must not get a strike.
        raise CareersError(f"HTTP {resp.status_code}: {url}", status=resp.status_code)
    # Unreachable: at attempt == MAX_RETRIES every branch above raises or
    # returns. Kept as a guard.
    raise CareersError(f"exhausted retries: {url}")  # pragma: no cover


def _retry_after(value: str | None) -> float | None:
    """Seconds a Retry-After header asks for, capped at MAX_RETRY_AFTER. Both
    header forms: delta-seconds and an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    wait = when.timestamp() - time.time()
    # A date already past says nothing useful; back off as if it were absent.
    return min(wait, MAX_RETRY_AFTER) if wait > 0 else None


DOWNLOAD_CHUNK = 1 << 20


def download(
    url: str, dest, timeout: int = REQUEST_TIMEOUT, deadline_ts: float | None = None
) -> int:
    """Stream url to the file `dest`, retrying a failed or 429/5xx start like
    `_fetch`. Returns the bytes written. A file this size never sits in memory."""

    def read(resp) -> int:
        size = 0
        with open(dest, "wb") as out:
            for chunk in resp.iter_content(DOWNLOAD_CHUNK):
                out.write(chunk)
                size += len(chunk)
        return size

    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            with _get(url, timeout=timeout, headers=_HEADERS, stream=True) as resp:
                _notify(url=url, secs=time.monotonic() - t0, status=resp.status_code)
                if resp.status_code == 200:
                    return read(resp)
                if (
                    not (resp.status_code == 429 or 500 <= resp.status_code < 600)
                    or attempt == MAX_RETRIES
                ):
                    raise CareersError(f"HTTP {resp.status_code}: {url}", status=resp.status_code)
        except requests.RequestException as exc:
            _notify(url=url, secs=time.monotonic() - t0, status=None)
            if attempt == MAX_RETRIES:
                raise CareersError(f"download failed: {url}: {exc}") from exc
        _sleep_or_raise(RETRY_BASE_DELAY * (2**attempt), deadline_ts, url)
    raise CareersError(f"exhausted retries: {url}")  # pragma: no cover


def fetch_page(url: str, timeout: int = REQUEST_TIMEOUT) -> tuple[int, str]:
    """(status, body) for one plain GET, no retries: for a careers page whose
    status the caller wants to explain itself."""
    t0 = time.monotonic()
    try:
        resp = _get(url, timeout=timeout, headers=_HEADERS)
    except requests.RequestException:
        _notify(url=url, secs=time.monotonic() - t0, status=None)
        raise
    _notify(url=url, secs=time.monotonic() - t0, status=resp.status_code)
    return resp.status_code, resp.text or ""


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
