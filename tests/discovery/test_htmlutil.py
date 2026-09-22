"""Coverage for the single HTML -> text path every board and `ingest_url` share."""
import logging

from src.discovery import htmlutil
from src.discovery.htmlutil import html_to_text


class TestNonText:
    def test_non_string_is_empty(self):
        assert html_to_text(None) == ""
        assert html_to_text(123) == ""
        assert html_to_text(["<p>x</p>"]) == ""

    def test_blank_is_empty(self):
        assert html_to_text("") == ""
        assert html_to_text("   \n\t ") == ""


class TestStructure:
    def test_list_items_become_dashes(self):
        text = html_to_text("<ul><li>One</li><li>Two</li></ul>")
        assert "- One" in text
        assert "- Two" in text

    def test_headings_become_markdown(self):
        assert html_to_text("<h2>Role</h2>").startswith("## Role")

    def test_br_becomes_newline(self):
        assert html_to_text("a<br/>b") == "a\nb"

    def test_block_ends_break_lines(self):
        assert html_to_text("<p>a</p><p>b</p>") == "a\nb"

    def test_unknown_tags_are_stripped(self):
        assert html_to_text("<span>a</span><em>b</em>") == "a b"

    def test_runs_of_blank_lines_collapse(self):
        assert "\n\n\n" not in html_to_text("<p>a</p><br/><br/><br/><p>b</p>")

    def test_runs_of_spaces_collapse(self):
        assert html_to_text("<p>a     b</p>") == "a b"


class TestEntities:
    def test_double_encoding_recovered(self):
        # Greenhouse double-encodes `content`.
        assert html_to_text("&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;") == "Build & ship"

    def test_single_pass_entities(self):
        assert html_to_text("<p>A &amp; B</p>") == "A & B"

    def test_nbsp_does_not_leak_markup(self):
        assert "&nbsp;" not in html_to_text("<p>a&nbsp;b</p>")


class TestTruncation:
    def test_under_limit_is_untouched(self, caplog):
        body = "x" * (htmlutil._MAX_DESCRIPTION_CHARS - 10)
        with caplog.at_level(logging.WARNING, logger=htmlutil.__name__):
            out = html_to_text(f"<p>{body}</p>")
        assert out == body
        assert caplog.records == []

    def test_over_limit_truncates_and_warns(self, caplog):
        body = "y" * (htmlutil._MAX_DESCRIPTION_CHARS + 500)
        with caplog.at_level(logging.WARNING, logger=htmlutil.__name__):
            out = html_to_text(f"<p>{body}</p>")
        assert len(out) == htmlutil._MAX_DESCRIPTION_CHARS
        assert out == body[: htmlutil._MAX_DESCRIPTION_CHARS]
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "truncated" in caplog.records[0].getMessage()

    def test_exactly_at_limit_does_not_warn(self, caplog):
        body = "z" * htmlutil._MAX_DESCRIPTION_CHARS
        with caplog.at_level(logging.WARNING, logger=htmlutil.__name__):
            out = html_to_text(f"<p>{body}</p>")
        assert len(out) == htmlutil._MAX_DESCRIPTION_CHARS
        assert caplog.records == []
