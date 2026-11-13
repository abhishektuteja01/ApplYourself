"""The answer bank's identity, education and employment facts that
profile/resume.yaml already states, read from it so they are typed once.

A key the bank sets wins; this fills only the keys it leaves out. A resume
line this cannot read gives no value, and the bank must set that key.
"""

from __future__ import annotations

import calendar
import re
from pathlib import Path
from typing import Any

import yaml

from applyourself.shared import schemas

_MONTHS = {m.lower()[:3]: m for m in calendar.month_name if m}
_SPAN = re.compile(
    r"^\s*(?P<sm>[A-Za-z]+)\.?\s+(?P<sy>\d{4})\s*[-–—]\s*"
    r"(?:(?P<em>[A-Za-z]+)\.?\s+(?P<ey>\d{4})|(?P<now>present|current|now))\s*$",
    re.IGNORECASE,
)


def _span(dates: Any) -> dict[str, Any]:
    """`Sep 2024 - May 2026` as start/end month (full name) and year; {} when unreadable."""
    m = _SPAN.match(str(dates or ""))
    if not m:
        return {}
    sm = _MONTHS.get(m["sm"].lower()[:3])
    if sm is None:
        return {}
    out: dict[str, Any] = {"start_month": sm, "start_year": m["sy"]}
    if m["now"]:
        out["current_role"] = True
        return out
    em = _MONTHS.get(m["em"].lower()[:3])
    if em is None:
        return {}
    out.update(end_month=em, end_year=m["ey"], current_role=False)
    return out


def _contact(resume: dict, scheme: str) -> str:
    for c in resume.get("contact") or []:
        if isinstance(c, dict) and str(c.get("url") or "").lower().startswith(scheme):
            return str(c.get("text") or "").strip()
    return ""


def identity(resume: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    name = str(resume.get("name") or "").split()
    if len(name) >= 2:
        out.update(first_name=name[0], last_name=" ".join(name[1:]), preferred_name=name[0])
    for key, scheme in (("email", "mailto:"), ("phone", "tel:")):
        if value := _contact(resume, scheme):
            out[key] = value
    entries = [c for c in resume.get("contact") or [] if isinstance(c, dict)]
    plain = [
        str(c.get("text") or "").strip()
        for c in sorted(entries, key=lambda c: c.get("location") is not True)
        if not c.get("url")
    ]
    if plain and plain[0]:
        out["location"] = plain[0]
    return out


def education(resume: dict) -> dict[str, str]:
    """From the first `education` entry: school (its detail up to the first
    comma or `|`), discipline (the degree after ` in `), and its dates."""
    entries = resume.get("education") or []
    if not entries or not isinstance(entries[0], dict):
        return {}
    e = entries[0]
    out: dict[str, Any] = {}
    school = re.split(r"[,|]", str(e.get("detail") or ""), maxsplit=1)[0].strip()
    if school:
        out["school"] = school
    degree = str(e.get("degree") or "").strip()
    if degree:
        out["degree"] = degree
    _, sep, discipline = degree.partition(" in ")
    if sep and discipline.strip():
        out["discipline"] = re.sub(r"\s*\(.*\)\s*$", "", discipline).strip()
    span = _span(e.get("dates"))
    span.pop("current_role", None)
    return {**out, **span}


def employment(resume: dict) -> dict[str, Any]:
    """From the `work` context that ended last (a current one first)."""
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    months = list(_MONTHS.values())
    for c in (resume.get("contexts") or {}).values():
        if not isinstance(c, dict) or c.get("section") != "work":
            continue
        span = _span(c.get("dates"))
        if not span:
            continue
        end = (
            (10**4, 0)
            if span["current_role"]
            else (int(span["end_year"]), months.index(span["end_month"]))
        )
        facts = {
            "company_name": str(c.get("org") or "").strip(),
            "title": str(c.get("title") or "").strip(),
            **span,
        }
        if best is None or end > best[0]:
            best = (end, {k: v for k, v in facts.items() if v != ""})
    return best[1] if best else {}


def read(path: Path) -> dict:
    """resume.yaml as a mapping; {} when it is absent."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    schemas.check(data.get("schema_version"), schemas.RESUME_SCHEMA_VERSION, path)
    return data
