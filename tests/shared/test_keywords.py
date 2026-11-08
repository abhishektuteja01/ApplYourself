"""keywords.py: spellings of one keyword share a key; different keywords never do."""

from __future__ import annotations

import pytest

from applyourself.shared import keywords

FOLD = keywords.variants()


@pytest.mark.parametrize(
    "a, b",
    [
        ("ＬＬＭ", "llm"),
        ("GPUs", "gpus"),
        ("SAAS", "SaaS"),
        ("KUBERNETES", "kubernetes"),
    ],
)
def test_case_never_counts(a, b):
    assert keywords.key(a, FOLD) == keywords.key(b, FOLD)


@pytest.mark.parametrize(
    "a, b",
    [
        ("GPU", "GPUs"),
        ("API", "APIs"),
        ("KPI", "KPIs"),
        ("ID", "IDs"),
        ("LLM", "LLMs"),
        ("cache", "caches"),
        ("batch", "batches"),
        ("cookie", "cookies"),
        ("policy", "policies"),
        ("database", "Databases"),
        ("process", "processes"),
    ],
)
def test_a_plural_folds_to_its_singular(a, b):
    assert keywords.key(a, FOLD) == keywords.key(b, FOLD)


@pytest.mark.parametrize(
    "a, b",
    [
        ("LLMs", "large language models"),
        ("evals", "Evaluations"),
        ("fine-tuning", "fine tuning"),
        ("full-stack", "Fullstack"),
        ("A.I.", "artificial intelligence"),
        ("Node.js", "NodeJS"),
        ("analysis", "analyses"),
    ],
)
def test_spellings_of_one_keyword_share_a_key(a, b):
    assert keywords.key(a, FOLD) == keywords.key(b, FOLD)


@pytest.mark.parametrize(
    "a, b",
    [
        ("AI", "a"),
        ("C", "C++"),
        ("C", "C#"),
        ("C", "R"),
        ("go", "going"),
        ("go", "goes"),
        ("embeddings", "embedded"),
        ("controls", "Controlling"),
        ("S&OP", "SOP"),
        ("production", "product"),
        ("marketing", "market"),
        ("accounting", "account"),
        ("statement", "state"),
        ("news", "new"),
        ("hosting", "host"),
        ("united", "unit"),
        ("Dynamics", "dynamic"),
        ("IA", "AI"),
        ("securities", "security"),
        ("customs", "custom"),
        ("teams", "team"),
        ("options", "option"),
    ],
)
def test_different_keywords_keep_apart(a, b):
    assert keywords.key(a, FOLD) != keywords.key(b, FOLD)


@pytest.mark.parametrize(
    "text, keyword, hit",
    [
        ("Judged runs with LLM-as-a-judge evals", "LLM-as-a-judge", True),
        ("Rolled out SAP S/4HANA", "S/4HANA", True),
        ("Built A.I. tools", "AI", True),
        ("Shipped full stack apps", "fullstack", True),
        ("Served LLMs in production", "LLM", True),
        ("HTML engineer", "ML", False),
        ("Wrote C++ only", "C", False),
        ("re act", "React", False),
        ("going far", "go", False),
        ("Built a I/O layer", "AI", False),
        ("Plan a, i.e. ship", "AI", False),
        ("C, I", "CI", False),
        ("Machine. Learning was", "ML", False),
        ("Trained machine learning models", "ML", True),
    ],
)
def test_contains_matches_whole_words_in_any_spelling(text, keyword, hit):
    assert keywords.contains(text, keyword, FOLD) is hit
