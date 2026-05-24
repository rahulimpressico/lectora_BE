"""
Extract text from PDF files using pypdf.

Returns plain text suitable for merging with DOCX content in A0.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_pdf_text(pdf_path: str, max_words: int = 10_000) -> str:
    """Extract all text from a PDF file, up to max_words words.

    Returns an empty string if the file cannot be read or has no extractable text.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("[pdf_extractor] pypdf not installed — PDF text extraction skipped.")
        return ""

    path = Path(pdf_path)
    if not path.is_file():
        logger.warning("[pdf_extractor] PDF not found: %s", pdf_path)
        return ""

    try:
        reader = PdfReader(str(path))
        pages: list[str] = []
        word_count = 0
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            pages.append(text)
            word_count += len(text.split())
            if word_count >= max_words:
                break
        return "\n\n".join(pages)
    except Exception as exc:
        logger.warning("[pdf_extractor] Failed to extract text from %s: %s", pdf_path, exc)
        return ""


def extract_pdf_indexed_content(pdf_path: str, max_words: int = 8_000) -> str:
    """Extract PDF text with [P<N>] paragraph markers, matching the DOCX indexed format.

    Used by A0 when a PDF is the primary document and no DOCX is available.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""

    path = Path(pdf_path)
    if not path.is_file():
        return ""

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        logger.warning("[pdf_extractor] Could not open PDF %s: %s", pdf_path, exc)
        return ""

    paragraphs: list[str] = []
    idx = 0
    word_count = 0

    for page in reader.pages:
        raw = (page.extract_text() or "").strip()
        if not raw:
            continue
        # Split on blank lines — rough paragraph segmentation for PDFs.
        chunks = re.split(r"\n{2,}", raw)
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            paragraphs.append(f"[P{idx}] {chunk}")
            word_count += len(chunk.split())
            idx += 1
            if word_count >= max_words:
                return "\n".join(paragraphs)

    return "\n".join(paragraphs)


def extract_pdf_learning_objectives(pdf_path: str) -> list[str]:
    """Best-effort extraction of learning objectives from a PDF.

    Looks for common trigger phrases and collects items that follow.
    Returns an empty list if nothing is found.
    """
    full_text = extract_pdf_text(pdf_path, max_words=5_000)
    if not full_text:
        return []

    _TRIGGER = re.compile(
        r"(?:learning\s+objectives?|learning\s+outcomes?|course\s+objectives?|upon\s+completion)",
        re.IGNORECASE,
    )
    _BULLET = re.compile(r"^[\-•\*·]\s+(.+)$")
    _NUMBERED = re.compile(r"^\d+[\.\)]\s+(.+)$")

    lines = full_text.splitlines()
    objectives: list[str] = []
    capturing = False

    for line in lines:
        stripped = line.strip()
        if _TRIGGER.search(stripped):
            capturing = True
            continue
        if capturing:
            m = _BULLET.match(stripped) or _NUMBERED.match(stripped)
            if m:
                objectives.append(m.group(1).strip())
            elif stripped and not objectives:
                # Prose objectives before any bullets
                if len(stripped) > 15:
                    objectives.append(stripped)
            elif not stripped and objectives:
                break

    return objectives[:20]


def get_pdf_title(pdf_path: str) -> str:
    """Extract the PDF title from metadata, or fall back to filename stem."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        meta = reader.metadata
        if meta and meta.title and len(meta.title.strip()) > 2:
            return meta.title.strip()
    except Exception:
        pass
    stem = Path(pdf_path).stem
    return " ".join(w.capitalize() for w in stem.replace("_", " ").split())


# Patterns that strongly suggest a heading line in plain PDF text.
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)[\s.\-:]\s*(.+)$")
_ALL_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9 \-/&,.:]{4,}$")
_TITLE_CASE_SHORT_RE = re.compile(r"^(?:[A-Z][a-z]+(?: [A-Z]?[a-z]*)+)$")


def extract_pdf_heading_tree(pdf_path: str, source_label: str = "pdf") -> list[dict]:
    """Detect heading-like lines in a PDF and return a structured heading tree.

    Uses heuristics since PDFs don't carry semantic style information:
    - Numbered headings: "7.0 Topic Name" / "2.1 Sub-topic"
    - ALL-CAPS short lines (typical section headers)
    - Title-case short lines at the start of a page

    Returns list of {level, text, para_idx, source} dicts (para_idx = line number).
    """
    full_text = extract_pdf_text(pdf_path, max_words=15_000)
    if not full_text:
        return []

    headings: list[dict] = []
    seen: set[str] = set()
    lines = full_text.splitlines()

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or len(line) > 120:
            continue

        key = line.lower()
        if key in seen:
            continue

        # Numbered heading: "3.0 Overview" → level 1, "3.1 Sub" → level 2
        m = _NUMBERED_HEADING_RE.match(line)
        if m:
            dots = m.group(1).count(".")
            level = dots + 1
            headings.append({"level": level, "text": line, "para_idx": idx, "source": source_label})
            seen.add(key)
            continue

        # ALL-CAPS short line (≤ 80 chars) — treated as level-1 heading
        if len(line) <= 80 and _ALL_CAPS_RE.match(line):
            headings.append({"level": 1, "text": line, "para_idx": idx, "source": source_label})
            seen.add(key)

    return headings
