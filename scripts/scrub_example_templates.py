"""Strip identity and Word cruft from the two committed example templates.

    uv run python scripts/scrub_example_templates.py [--check]

The templates are hand-authored in Word, so every save stamps the editor's name
into docProps/core.xml and their company into docProps/app.xml. These two files
are the repo's only tracked binaries and are allowlisted by name in
scripts/pii_scan.sh, so the text scan that protects every other tracked file
does not see them. Run this after any Word edit; tests/test_example_templates.py
verifies the result.

What it removes:
  core.xml    -- creator, lastModifiedBy, title, subject, description, keywords,
                 category.
  app.xml     -- Company, Manager, HyperlinkBase, and Template (Word writes the
                 GUID of the personal .dotx the document was authored from).
  thumbnail   -- docProps/thumbnail.jpeg and its relationship. It carries no
                 personal data, but it is an image the PII text scan cannot read
                 inside a file the gate allowlists.
  w:sdt       -- content controls are unwrapped to their contents. Word binds
                 them to core properties (w:dataBinding), so a control bound to
                 cp:keywords re-syncs its text from a field this script empties
                 -- silently blanking a placeholder the renderer requires.

--check exits 1 without writing if either file still needs scrubbing. The gate CI
and the pre-push hook actually run is tests/test_example_templates.py.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from src import paths

RESUME = paths.REPO_ROOT / "profile" / "resume_template.example.docx"
COVER = paths.REPO_ROOT / "profile" / "cover_letter_template.example.docx"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

THUMBNAIL_PART = "docProps/thumbnail.jpeg"
_THUMBNAIL_REL_RE = re.compile(
    r'<Relationship[^>]*Target="docProps/thumbnail\.jpeg"[^>]*/>'
)

# core.xml elements to empty, by qualified tag name as Word writes them.
CORE_FIELDS = (
    "dc:creator",
    "cp:lastModifiedBy",
    "dc:title",
    "dc:subject",
    "dc:description",
    "cp:keywords",
    "cp:category",
)

# app.xml elements to empty. Template is included because Word writes the GUID
# of the personal .dotx the document was authored from.
APP_FIELDS = ("Company", "Manager", "HyperlinkBase", "Template")


def _empty_elements(xml: str, fields: tuple[str, ...]) -> str:
    """Blank each field's text, keeping the element so the part stays valid."""
    for field in fields:
        xml = re.sub(
            rf"<{field}(\s[^>]*)?>.*?</{field}>",
            lambda m, f=field: f"<{f}{m.group(1) or ''}></{f}>",
            xml,
            flags=re.S,
        )
    return xml


def _unwrap_content_controls(xml_bytes: bytes) -> bytes:
    """Replace every w:sdt with the children of its w:sdtContent.

    Deepest-first so nested controls collapse correctly.
    """
    root = etree.fromstring(xml_bytes)
    sdts = root.findall(f".//{{{W_NS}}}sdt")
    if not sdts:
        return xml_bytes
    for sdt in reversed(sdts):
        parent = sdt.getparent()
        if parent is None:
            continue
        content = sdt.find(f"{{{W_NS}}}sdtContent")
        index = list(parent).index(sdt)
        if content is not None:
            for child in reversed(list(content)):
                parent.insert(index, child)
        parent.remove(sdt)
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _scrub_bytes(name: str, data: bytes) -> bytes:
    if name == "docProps/core.xml":
        return _empty_elements(data.decode("utf-8"), CORE_FIELDS).encode("utf-8")
    if name == "docProps/app.xml":
        return _empty_elements(data.decode("utf-8"), APP_FIELDS).encode("utf-8")
    if name == "_rels/.rels":
        return _THUMBNAIL_REL_RE.sub("", data.decode("utf-8")).encode("utf-8")
    if name.startswith("word/") and name.endswith(".xml"):
        return _unwrap_content_controls(data)
    return data


def scrub(path: Path, *, write: bool = True) -> bool:
    """Rewrite `path` scrubbed. Returns True if anything changed.

    write=False reports what would change without touching `path` — a --check
    that rewrote the file and restored it from a backup left the repo altered if
    it was interrupted."""
    with zipfile.ZipFile(path) as zf:
        entries = [(info, zf.read(info.filename)) for info in zf.infolist()]

    changed = False
    with tempfile.NamedTemporaryFile(
        dir=path.parent, suffix=".docx", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as out:
            for info, data in entries:
                if info.filename == THUMBNAIL_PART:
                    changed = True
                    continue
                scrubbed = _scrub_bytes(info.filename, data)
                if scrubbed != data:
                    changed = True
                out.writestr(info, scrubbed)
        if changed and write:
            shutil.move(str(tmp_path), str(path))
    finally:
        tmp_path.unlink(missing_ok=True)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if either template still needs scrubbing; write nothing",
    )
    args = parser.parse_args()

    dirty: list[Path] = []
    for path in (RESUME, COVER):
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 2
        if args.check:
            if scrub(path, write=False):
                dirty.append(path)
        elif scrub(path):
            dirty.append(path)
            print(f"scrubbed {path.relative_to(paths.REPO_ROOT)}")
        else:
            print(f"clean    {path.relative_to(paths.REPO_ROOT)}")

    if args.check and dirty:
        names = ", ".join(str(p.relative_to(paths.REPO_ROOT)) for p in dirty)
        print(
            f"needs scrubbing: {names}\n"
            f"run: uv run python scripts/scrub_example_templates.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
