"""
Extract Table of Contents (TOC) entries from a DOCX Document.

Microsoft Word renders an inserted TOC as a sequence of paragraphs styled
"TOC 1", "TOC 2", "TOC 3", … (one style level per heading depth).  The text
in each paragraph has the form:

    Heading Title<tab><tab>42

where the trailing tab + digits is the page-number reference.  This module
strips that page-number noise and returns clean, level-tagged TOC entries.

Two helpers are exposed for the rest of A0:

    extract_toc_entries_from_doc(doc, source_label)
        → low-level, operates on a single python-docx Document object

    toc_entries_to_heading_tree(entries)
        → converts TOCEntry objects to the {level, text, para_idx, source}
          heading_tree format used in shared_state

Usage inside CourseDocParser::
    from .toc_extractor import extract_toc_entries_from_doc, TOCEntry
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from docx import Document  # type: ignore[import-untyped]
else:
    Document = Any


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# "TOC 1", "TOC 2", "toc1", "Toc 3" …
_TOC_LEVEL_RE = re.compile(r"^toc\s*(\d+)$", re.IGNORECASE)

# "TOC Heading" — the "Contents" / "Table of Contents" title paragraph itself;
# we skip it because it is not a content entry.
_TOC_HEADING_STYLE_RE = re.compile(r"^toc[\s\-_]*heading$", re.IGNORECASE)

# Page-number trailer at the end of a TOC paragraph:
# one or more tabs/spaces followed by one or more digits (+ optional trailing whitespace)
_PAGE_TRAIL_RE = re.compile(r"[\t ]+\d[\d\s]*$")


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class TOCEntry:
    """A single entry from a DOCX Table of Contents.

    Attributes
    ----------
    level:  Nesting depth (1 = top-level section, 2 = sub-section, etc.)
    text:   Heading text with page-number stripped.
    page:   Parsed page number if available; None otherwise.
    source: Source filename (for multi-document scenarios).
    """
    level: int
    text: str
    page: Optional[int] = None
    source: str = ""


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_toc_entries_from_doc(
    doc: Document,
    source_label: str = "",
) -> list[TOCEntry]:
    """Extract all TOC entries from a single python-docx Document object.

    Scans every paragraph whose style matches ``TOC N`` (case-insensitive,
    optional space between "TOC" and the digit).  Skips the "TOC Heading"
    paragraph (the literal heading of the TOC block such as "Contents").

    Args:
        doc:          A loaded python-docx ``Document`` instance.
        source_label: Optional filename / identifier used to tag each entry.

    Returns:
        List of :class:`TOCEntry` objects in document order.  Empty when the
        document contains no TOC paragraphs.
    """
    entries: list[TOCEntry] = []

    for para in doc.paragraphs:
        style_name = (para.style.name or "").strip()

        # Skip the TOC section title itself ("Contents", "Table of Contents")
        if _TOC_HEADING_STYLE_RE.match(style_name):
            continue

        m = _TOC_LEVEL_RE.match(style_name)
        if not m:
            continue

        level = int(m.group(1))
        raw_text = para.text

        # Extract page number before stripping
        page: Optional[int] = None
        trail = _PAGE_TRAIL_RE.search(raw_text)
        if trail:
            digits = re.search(r"\d+", trail.group(0))
            if digits:
                try:
                    page = int(digits.group(0))
                except ValueError:
                    pass

        # Remove page-number trailer and clean up
        clean_text = _PAGE_TRAIL_RE.sub("", raw_text).strip()
        if not clean_text:
            continue

        entries.append(
            TOCEntry(level=level, text=clean_text, page=page, source=source_label)
        )

    return entries


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def toc_entries_to_heading_tree(entries: list[TOCEntry]) -> list[dict]:
    """Convert TOC entries to the heading_tree dict format.

    The heading_tree format is ``{level, text, para_idx, source}`` and is
    Used for heading_tree metadata in shared_state (not for LLM TO generation).

    ``para_idx`` is set to ``-1`` because TOC entries carry no body-paragraph
    index — the actual body indices are resolved later by
    :meth:`~doc_parser.CourseDocParser.extract_toc_section_contents`.

    Args:
        entries: List of :class:`TOCEntry` objects.

    Returns:
        List of heading_tree-compatible dicts.
    """
    return [
        {
            "level": e.level,
            "text": e.text,
            "para_idx": -1,
            "source": e.source or "toc",
        }
        for e in entries
    ]


def toc_entries_to_hierarchy(entries: list[TOCEntry]) -> list[dict[str, Any]]:
    """Convert a flat TOC entry list into a nested hierarchy.

    The conversion is fully level-driven, so it works for any numbering scheme
    or heading labels as long as ``TOCEntry.level`` is populated.
    """
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []

    for entry in entries:
        node = {
            "level": entry.level,
            "text": entry.text,
            "page": entry.page,
            "source": entry.source or "toc",
            "children": [],
        }

        while stack and stack[-1]["level"] >= entry.level:
            stack.pop()

        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)

        stack.append(node)

    return roots
