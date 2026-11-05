"""Referral lookup by company (src/contacts.py)."""

from __future__ import annotations

import yaml

from src import contacts


def _write(path, entries):
    path.write_text(yaml.safe_dump({"schema_version": 1, "contacts": entries}), encoding="utf-8")
    return path


def test_missing_file_means_no_referrals(tmp_path):
    assert contacts.referrals_by_company(tmp_path / "contacts.yaml") == {}


def test_matches_after_company_cleanup_and_skips_other_relationships(tmp_path):
    path = _write(
        tmp_path / "contacts.yaml",
        [
            {
                "id": "a",
                "name": "Sam Okafor",
                "company": "Widgets R Us",
                "relationship": "referral",
            },
            {"id": "b", "name": "Lee Park", "company": "Widgets R Us", "relationship": "referral"},
            {
                "id": "c",
                "name": "Dana Reyes",
                "company": "Widgets R Us",
                "relationship": "recruiter",
            },
        ],
    )
    index = contacts.referrals_by_company(path)
    assert contacts.referral_note("Widgets R Us, Inc.", index) == "referral: Sam Okafor, Lee Park"
    assert contacts.referral_note("Sprocket Labs", index) == ""
