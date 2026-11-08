"""keywords.py: spellings of one keyword share a key; different keywords never do."""

from __future__ import annotations

import pytest

from applyourself.shared import keywords

FOLD = keywords.variants()


@pytest.mark.parametrize(
    "a, b",
    [
        ("LLM", "LLMs"),
        ("LLMs", "large language models"),
        ("evals", "Evaluations"),
        ("fine-tuning", "fine tuning"),
        ("full-stack", "Fullstack"),
        ("A.I.", "artificial intelligence"),
        ("ＬＬＭ", "llm"),
        ("GPUs", "gpus"),
        ("SAAS", "SaaS"),
        ("KUBERNETES", "kubernetes"),
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
    ],
)
def test_contains_matches_whole_words_in_any_spelling(text, keyword, hit):
    assert keywords.contains(text, keyword, FOLD) is hit
