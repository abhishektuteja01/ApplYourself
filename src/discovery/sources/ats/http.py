import logging
import time
from datetime import date, datetime
import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
REQUEST_TIMEOUT = 30
_HEADERS = {"User-Agent": "auto-app-filler (personal job-search pipeline)"}

class CareersError(Exception):
    """Board fetch failed after retries, or the board does not exist."""

def fetch_json(url: str, timeout: int = REQUEST_TIMEOUT):
    """GET url -> parsed JSON. Retries 429/5xx (honoring Retry-After)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers=_HEADERS)
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise CareersError(f"fetch failed: {url}: {exc}") from exc
            time.sleep(RETRY_BASE_DELAY * attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            raise CareersError(f"board not found (404) — check the slug: {url}")
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt == MAX_RETRIES:
                raise CareersError(
                    f"HTTP {resp.status_code} after {MAX_RETRIES} attempts: {url}"
                )
            retry_after = resp.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else RETRY_BASE_DELAY * (2 ** attempt)
            )
            time.sleep(delay)
            continue
        raise CareersError(f"HTTP {resp.status_code}: {url}")
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
