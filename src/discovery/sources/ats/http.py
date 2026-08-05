import logging
import time
from datetime import date, datetime
import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
REQUEST_TIMEOUT = 30
# Retry-After is remote input; a board asking for an hour gets skipped instead.
MAX_RETRY_AFTER = 60
_HEADERS = {"User-Agent": "job-search-pipeline (personal job-search pipeline)"}

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

def fetch_json(
    url: str,
    timeout: int = REQUEST_TIMEOUT,
    deadline_ts: float | None = None,
):
    """GET url -> parsed JSON. Retries 429/5xx (Retry-After capped at MAX_RETRY_AFTER)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers=_HEADERS)
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise CareersError(f"fetch failed: {url}: {exc}") from exc
            _sleep_or_raise(RETRY_BASE_DELAY * attempt, deadline_ts, url)
            continue
        if resp.status_code == 200:
            # A non-JSON 200 is a wall or a wrong slug, not a blip: no retry.
            try:
                return resp.json()
            except ValueError as exc:
                raise CareersError(
                    f"invalid JSON body: {url}: {exc}", status=200, permanent=True
                ) from exc
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
    raise CareersError(f"exhausted retries: {url}")

def iso_date(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None

def ms_date(value) -> date | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000).date()
    except (ValueError, OSError, OverflowError):
        return None
