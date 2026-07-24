import html as html_mod
import re

_BR_RE = re.compile(r"<br\s*/?>", re.I)
_LI_RE = re.compile(r"<li[^>]*>", re.I)
_HEAD_RE = re.compile(r"<h[1-6][^>]*>", re.I)
_BLOCK_END_RE = re.compile(r"</(?:p|div|li|ul|ol|h[1-6]|tr|table|section|blockquote)>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t]{2,}")
_BLANKS_RE = re.compile(r"\n{3,}")

_MAX_DESCRIPTION_CHARS = 25_000

def html_to_text(value) -> str:
    """HTML (possibly entity-encoded — Greenhouse double-encodes `content`)
    -> markdown-ish plain text: list items become `- `, headings `## `."""
    if not isinstance(value, str) or not value.strip():
        return ""
    x = html_mod.unescape(value)  # first pass recovers real tags
    x = _BR_RE.sub("\n", x)
    x = _LI_RE.sub("\n- ", x)
    x = _HEAD_RE.sub("\n\n## ", x)
    x = _BLOCK_END_RE.sub("\n", x)
    x = _TAG_RE.sub(" ", x)
    x = html_mod.unescape(x)  # second pass: entities inside the text
    lines = [_SPACES_RE.sub(" ", ln.strip()) for ln in x.splitlines()]
    x = _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()
    return x[:_MAX_DESCRIPTION_CHARS]
