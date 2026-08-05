"""Coverage for the manual-clip source: parse_inbox_file's reject paths and
ingest_inbox's move-on-read side effect (which, unisolated, eats real clips)."""

from pathlib import Path

import pytest

from src.discovery import cleaning, inbox

REPO_INBOX = Path(__file__).resolve().parent.parent.parent / "inbox"

VALID = """---
company: Acme Corp
title: Machine Learning Engineer
url: https://acme.example/jobs/1
location: Boston, MA
vertical: example_tertiary
---
We need someone to build model pipelines.
"""


def write(dirpath: Path, name: str, text: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------
# parse_inbox_file
# ---------------------------------------------------------------------

def test_parse_valid_clip(tmp_path, cfg):
    row = inbox.parse_inbox_file(write(tmp_path, "a.md", VALID))
    assert row is not None
    assert row["site"] == "manual"
    assert row["company"] == "Acme Corp"
    assert row["title"] == "Machine Learning Engineer"
    assert row["job_url"] == "https://acme.example/jobs/1"
    # url_direct defaults to url
    assert row["job_url_direct"] == "https://acme.example/jobs/1"
    assert row["description"] == "We need someone to build model pipelines.\n"


def test_parse_frontmatter_vertical_wins_over_classifier(tmp_path, cfg):
    """An explicit vertical: is taken verbatim, even when the title would
    classify elsewhere or not at all."""
    text = VALID.replace(
        "title: Machine Learning Engineer", "title: Totally Unclassifiable Role"
    )
    assert cleaning.classify_vertical_from_title("Totally Unclassifiable Role") == ""
    row = inbox.parse_inbox_file(write(tmp_path, "a.md", text))
    assert row["vertical"] == "example_tertiary"


def test_parse_missing_vertical_falls_back_to_classifier(tmp_path, cfg):
    text = VALID.replace("vertical: example_tertiary\n", "")
    row = inbox.parse_inbox_file(write(tmp_path, "a.md", text))
    assert row["vertical"] == cleaning.classify_vertical_from_title(
        "Machine Learning Engineer"
    )


@pytest.mark.parametrize(
    "name,text",
    [
        ("no_frontmatter", "company: Acme\ntitle: X\nurl: y\n"),
        ("unclosed_fence", "---\ncompany: Acme\ntitle: X\nurl: y\n"),
        ("bad_yaml", "---\ncompany: [unclosed\n---\nbody\n"),
        ("not_a_mapping", "---\n- just\n- a list\n---\nbody\n"),
        ("missing_company", "---\ntitle: X\nurl: y\n---\nbody\n"),
        ("missing_title", "---\ncompany: Acme\nurl: y\n---\nbody\n"),
        ("missing_url", "---\ncompany: Acme\ntitle: X\n---\nbody\n"),
        ("blank_company", "---\ncompany: '   '\ntitle: X\nurl: y\n---\nbody\n"),
        ("nonstring_title", "---\ncompany: Acme\ntitle: 42\nurl: y\n---\nbody\n"),
    ],
)
def test_parse_rejects_malformed(tmp_path, cfg, name, text):
    assert inbox.parse_inbox_file(write(tmp_path, f"{name}.md", text)) is None


# ---------------------------------------------------------------------
# ingest_inbox
# ---------------------------------------------------------------------

def test_ingest_moves_valid_to_processed(tmp_path, cfg):
    d = tmp_path / "inbox"
    write(d, "a.md", VALID)
    df, counts = inbox.ingest_inbox(d)
    assert counts == {"processed": 1, "malformed": 0}
    assert len(df) == 1
    assert not (d / "a.md").exists()
    assert (d / ".processed" / "a.md").exists()


def test_ingest_moves_malformed_to_malformed(tmp_path, cfg):
    d = tmp_path / "inbox"
    write(d, "bad.md", "no frontmatter here\n")
    df, counts = inbox.ingest_inbox(d)
    assert counts == {"processed": 0, "malformed": 1}
    assert df.empty
    assert (d / ".malformed" / "bad.md").exists()


def test_ingest_destinations_derive_from_argument(tmp_path, cfg, monkeypatch):
    """Both destinations must come from inbox_dir, not from INBOX — otherwise a
    passed-in dir still writes its moves into the real inbox/."""
    monkeypatch.setattr(inbox, "INBOX", tmp_path / "nope")
    d = tmp_path / "inbox"
    write(d, "a.md", VALID)
    write(d, "bad.md", "no frontmatter here\n")
    inbox.ingest_inbox(d)
    assert (d / ".processed" / "a.md").exists()
    assert (d / ".malformed" / "bad.md").exists()
    assert not (tmp_path / "nope").exists()


def test_ingest_skips_dot_subdirs(tmp_path, cfg):
    """Already-moved clips must not be re-ingested on the next run."""
    d = tmp_path / "inbox"
    write(d / ".processed", "old.md", VALID)
    write(d / ".malformed", "bad.md", "junk\n")
    df, counts = inbox.ingest_inbox(d)
    assert counts == {"processed": 0, "malformed": 0}
    assert df.empty


def test_ingest_missing_dir_is_noop(tmp_path, cfg):
    df, counts = inbox.ingest_inbox(tmp_path / "absent")
    assert df.empty
    assert counts == {"processed": 0, "malformed": 0}


# ---------------------------------------------------------------------
# isolation: the default arg must resolve at call time
# ---------------------------------------------------------------------

def test_default_inbox_resolves_at_call_time(tmp_path, cfg, monkeypatch):
    """The autouse conftest fixture patches inbox.INBOX; a default bound at
    import would ignore it and consume the real inbox/."""
    d = tmp_path / "patched_inbox"
    write(d, "a.md", VALID)
    monkeypatch.setattr(inbox, "INBOX", d)
    df, counts = inbox.ingest_inbox()
    assert counts == {"processed": 1, "malformed": 0}
    assert (d / ".processed" / "a.md").exists()


def test_inbox_source_does_not_touch_the_repo_inbox(tmp_path, cfg, monkeypatch):
    """InboxSource.fetch() passes no argument, so the whole source has to honour
    the patched INBOX. Guards the real inbox/ against pytest runs."""
    before = sorted(p.name for p in REPO_INBOX.glob("*.md")) if REPO_INBOX.exists() else []
    d = tmp_path / "patched_inbox"
    write(d, "a.md", VALID)
    monkeypatch.setattr(inbox, "INBOX", d)
    result = inbox.InboxSource().fetch(ctx=None)
    assert len(result.rows) == 1
    assert result.rows[0]["company"] == "Acme Corp"
    assert result.report_lines == ["Inbox: processed=1, malformed=0"]
    after = sorted(p.name for p in REPO_INBOX.glob("*.md")) if REPO_INBOX.exists() else []
    assert before == after
