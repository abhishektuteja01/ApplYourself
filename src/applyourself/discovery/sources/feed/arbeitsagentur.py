"""Bundesagentur für Arbeit (arbeitsagentur.de): the JD of one posting. Not a
board lane; the feed's rows from it carry no JD and the job page is built in
the browser, so the JD comes from the job search's public detail API."""

from __future__ import annotations

import base64
import re
from urllib.parse import unquote

from applyourself.discovery import parse

API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/"
# The job search's own public client key, sent by its web page.
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}
# The posting's reference number is the page path's tail; it may hold a "/".
_JOB_RE = re.compile(r"^https?://(?:www\.)?arbeitsagentur\.de/jobsuche/jobdetail/([^?#]+)", re.I)


def detail_url(url: str) -> str:
    """The detail API url (the reference number, base64) for a job page url, or ""."""
    m = _JOB_RE.match((url or "").strip())
    ref = unquote(m.group(1)).strip("/") if m else ""
    return API + base64.b64encode(ref.encode()).decode() if ref else ""


def jd_text(payload) -> str:
    """The JD in a detail response, "" when there is none."""
    if not isinstance(payload, dict):
        return ""
    return parse.html_to_text(payload.get("stellenangebotsBeschreibung") or "")
