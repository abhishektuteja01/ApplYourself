"""Tests for the JobSpy source family — the google_search_term hook only.

Network fetching is not exercised here (jobspy is an external scraper); these
cover the one piece of branching logic the subclasses add. The `enabled` flag
is a runtime gate read only by the orchestrator, so it does not affect these.
"""
from __future__ import annotations

from src.discovery.sources.jobspy_source import (
    GoogleSource,
    IndeedSource,
    LinkedinSource,
    ZipRecruiterSource,
)


def test_google_builds_natural_language_query():
    g = GoogleSource()
    assert g.name == "google"
    assert g.google_search_term("AI Engineer", "Austin, TX", False) == (
        "AI Engineer jobs in Austin, TX"
    )
    assert g.google_search_term("AI Engineer", "Austin, TX", True) == (
        "AI Engineer jobs remote"
    )


def test_non_google_sources_pass_none():
    """jobspy keys every other site off search_term alone and requires None
    here; returning a string would corrupt those queries."""
    for src in (LinkedinSource(), IndeedSource(), ZipRecruiterSource()):
        assert src.google_search_term("AI Engineer", "Austin, TX", False) is None
        assert src.google_search_term("AI Engineer", "Austin, TX", True) is None
