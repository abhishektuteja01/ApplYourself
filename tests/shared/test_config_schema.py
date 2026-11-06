"""validate() names every problem: unknown keys and wrong types together."""

from __future__ import annotations

import pytest

from applyourself.shared.config_schema import ClassifierRule, Settings, validate


def test_a_type_error_is_listed_beside_an_unknown_key():
    with pytest.raises(ValueError) as exc:
        validate(Settings, {"retries": "many", "bogus": 1}, "settings.yaml")
    msg = str(exc.value)
    assert "unknown key 'bogus'" in msg and "settings.yaml: retries:" in msg


def test_an_unknown_key_in_a_union_member_is_not_also_a_type_error():
    with pytest.raises(ValueError) as exc:
        validate(ClassifierRule, {"pattern": {"match": "x", "bogus": 1}}, "v.yaml")
    assert str(exc.value).splitlines() == [
        "v.yaml: pattern: unknown key 'bogus'; valid keys: match, require_any"
    ]
