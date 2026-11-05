"""Cut boilerplate sections out of a job description."""
from __future__ import annotations

import re

_BULLET = ("-", "•", "·", "+", "* ")


def _heading_line(line: str) -> bool:
    """A marked heading: `# X`, `**X**` or `X:`, at most eight words."""
    t = line.strip()
    words = t.strip("#*_ :").split()
    return (0 < len(words) <= 8 and not t.startswith(_BULLET)
            and (t.startswith("#") or (t.startswith("**") and t.rstrip(":").endswith("**"))
                 or t.endswith(":")))


def _blocks(text: str) -> list[tuple[bool, str]]:
    """(starts with a heading, text) blocks: paragraphs, split again at every
    marked heading line. A lone short line with no end punctuation is a
    heading too."""
    out = []
    for par in re.split(r"\n\s*\n", text.strip()):
        lines = par.strip().splitlines()
        if len(lines) == 1 and not _heading_line(lines[0]):
            t = lines[0].strip()
            lone = (0 < len(t.split()) <= 8 and any(ch.isalpha() for ch in t)
                    and not t.startswith(_BULLET)
                    and not t.endswith((".", "!", "?", ",")))
            out.append((lone, t))
            continue
        cur: list[str] = []
        head = False
        for line in lines:
            if _heading_line(line) and cur:
                out.append((head, "\n".join(cur)))
                cur = []
            if not cur:
                head = _heading_line(line)
            cur.append(line)
        if cur:
            out.append((head, "\n".join(cur)))
    return out


def trim_boilerplate(text, headings, keep_words) -> str:
    """`text` without its boilerplate sections (EEO, benefits, about-us...).
    A section starts at a heading starting with one of `headings` and runs to
    the next heading. A block holding one of `keep_words` always stays."""
    if not isinstance(text, str):
        return ""
    heads = tuple(h.lower() for h in headings)
    keeps = tuple(k.lower() for k in keep_words)
    kept, cutting = [], False
    for is_head, block in _blocks(text):
        starts_boiler = block.lstrip("#*_ ").lower().startswith(heads)
        if is_head:
            cutting = starts_boiler
        low = block.lower()
        if (cutting or starts_boiler) and not any(k in low for k in keeps):
            continue
        kept.append(block.strip())
    return "\n\n".join(kept)
