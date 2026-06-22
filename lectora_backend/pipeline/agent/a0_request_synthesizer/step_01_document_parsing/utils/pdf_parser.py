"""
PDFSourceParser — extract A0-style source inputs directly from PDF files.

This module mirrors the subset of ``CourseDocParser`` that A0 uses for
classification and timed-outline generation:

- title / course_id
- learning_objectives
- content_sample
- indexed_content with [P<N>] markers
- heading_tree / heading_map
- TOC entries (from PDF bookmarks / outline)
- images with best-effort paragraph and heading context

Unlike DOCX parsing, PDF extraction is heuristic because PDFs usually preserve
layout, not semantic heading styles. We therefore rely on:

- bookmark / outline entries when present
- numbered-heading and ALL-CAPS heuristics
- pypdf image extraction and page-local context for captions
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .toc_extractor import TOCEntry, toc_entries_to_hierarchy

logger = logging.getLogger(__name__)

_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)[\s.\-:]\s*(.+)$")
_ALL_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9 \-/&(),.:]{3,100}$")
_COURSE_ID_RE = re.compile(r"Course\s*ID[:\s]*(\d+)", re.IGNORECASE)
_LO_TRIGGER_RE = re.compile(
    r"^(learning objectives?|course objectives?|learning outcomes?)\s*:?\s*$",
    re.IGNORECASE,
)
_BULLET_PREFIX_RE = re.compile(
    r"^[\s]*(?:"
    r"[•‣◦⁃∙▪●○∘·"
    r"◘►➤⁌⁍\-–—\*•▪▸►◦◇●]"
    r"[\s]*)+"
)
_NUMBERED_LO_RE = re.compile(
    r"^(?:\(?[0-9]+[.)]\s+|\(?[a-zA-Z][.)]\s+|[0-9]{1,}\s+(?=\S))"
)
_LO_FIRST_TOKEN_RE = re.compile(
    r"^(?P<first>[A-Za-z]{2,}(?:'[sS])?)(?=[\s\.,;:\/\-]|$)"
)
_LO_SKIP_FIRST_WORD = frozenset(
    {
        "the", "this", "these", "those", "when", "if", "for", "there", "here",
        "it", "its", "as", "while", "however", "although", "because", "since",
        "during", "after", "before", "by", "in", "on", "at", "an", "and", "or",
        "but", "nor", "we", "you", "they", "he", "she", "our", "your", "their",
        "some", "many", "most", "all", "each", "every", "such", "one", "two",
        "three", "both", "another", "other", "any", "no", "not", "only", "also",
        "from", "into", "with", "without", "within", "about", "above",
    }
)
_IMAGE_CAPTION_TRIGGER_RE = re.compile(
    r"\b(figure|fig\.|chart|graph|table|map|diagram|image|photo|illustration)\b",
    re.IGNORECASE,
)
_NUM_PREFIX_RE = re.compile(r"^\d+(\.\d+)*[\s.\-:]*")
_PAGE_TRAIL_RE = re.compile(r"[\t ]+\d[\d\s]*$")
_STRUCTURAL_TOC_RE = re.compile(
    r"^(?:table of contents|tables?|figures?|change record|list of tables|list of figures"
    r"|message to\b|preface|acknowledgments?)$",
    re.IGNORECASE,
)
_NOISE_HEADING_RE = re.compile(
    r"(?:is not a valid value|sfip page \d+ of \d+|^\d[\d.,\s]{8,}$|"
    r"^[A-Z]{1,5}$|^\d+\.\d+,)",
    re.IGNORECASE,
)
_FORM_FOOTER_RE = re.compile(r"\bSFIP\b.*\bPAGE\b", re.IGNORECASE)
# Max bookmark/heuristic TOC rows sent to LLM-facing structures (large manuals).
_MAX_TOC_ENTRIES_FOR_PROMPT = 400


@dataclass
class PDFBlock:
    para_idx: int
    page_num: int
    text: str
    heading_level: int | None


@dataclass
class PDFDocumentData:
    path: Path
    reader: PdfReader
    blocks: list[PDFBlock]
    toc_entries: list[TOCEntry]


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def infer_heading_level(text: str) -> int | None:
    text = normalize_ws(text)
    if not text or not _is_plausible_body_heading(text):
        return None
    m = _NUMBERED_HEADING_RE.match(text)
    if m:
        title_part = m.group(2).strip()
        if len(title_part) > 120 or not title_part:
            return None
        return m.group(1).count(".") + 1
    if len(text) <= 100 and _ALL_CAPS_RE.match(text):
        words = text.split()
        if len(words) >= 2 or len(text) >= 18:
            return 1
    return None


def _clean_toc_title(text: str) -> str:
    """Strip bookmark/page noise from TOC titles (same idea as DOCX TOC extractor)."""
    cleaned = _PAGE_TRAIL_RE.sub("", normalize_ws(text))
    return cleaned.strip()


def _is_structural_toc_title(text: str) -> bool:
    """Front-matter / index pages that are not course lessons."""
    cleaned = _clean_toc_title(text)
    if not cleaned:
        return True
    if _STRUCTURAL_TOC_RE.match(cleaned):
        return True
    lower = cleaned.lower()
    if lower.startswith("message to ") and "agent" in lower:
        return True
    return False


def _is_plausible_body_heading(text: str) -> bool:
    """Reject PDF line-break noise misclassified as headings."""
    t = normalize_ws(text)
    if len(t) < 4 or len(t) > 200:
        return False
    if _NOISE_HEADING_RE.search(t):
        return False
    if _FORM_FOOTER_RE.search(t):
        return False
    if t.endswith("-") and len(t.split()) <= 4:
        return False
    words = t.split()
    if len(words) == 1 and words[0].isupper() and len(words[0]) <= 10:
        return False
    return True


def _filter_toc_entries(entries: list[TOCEntry]) -> list[TOCEntry]:
    """Drop structural front-matter and dedupe bookmark rows."""
    filtered: list[TOCEntry] = []
    seen: set[tuple[str, int, str]] = set()
    for entry in entries:
        text = _clean_toc_title(entry.text)
        if not text or _is_structural_toc_title(text):
            continue
        key = (_norm_heading(text), entry.level, entry.source or "")
        if key in seen:
            continue
        seen.add(key)
        filtered.append(
            TOCEntry(
                level=entry.level,
                text=text,
                page=entry.page,
                source=entry.source,
            )
        )
    if len(filtered) > _MAX_TOC_ENTRIES_FOR_PROMPT:
        logger.info(
            "[pdf_parser] TOC capped: %d → %d entries (kept shallow levels first)",
            len(filtered),
            _MAX_TOC_ENTRIES_FOR_PROMPT,
        )
        filtered.sort(key=lambda e: (e.level, e.page or 0))
        filtered = filtered[:_MAX_TOC_ENTRIES_FOR_PROMPT]
    return filtered


def _norm_heading(text: str) -> str:
    return _NUM_PREFIX_RE.sub("", normalize_ws(text)).lower().strip()


def _strip_bullet_prefix(text: str) -> str:
    return _BULLET_PREFIX_RE.sub("", text).strip()


def _has_leading_bullet_marker(text: str) -> bool:
    return bool(_BULLET_PREFIX_RE.match(text)) and bool(_strip_bullet_prefix(text))


def _is_lo_intro_line(text: str, already_have_objectives: bool) -> bool:
    if already_have_objectives or len(text) > 220:
        return False
    return text.rstrip().endswith(":")


def _looks_like_prose_objective(text: str) -> bool:
    t = text.strip()
    if len(t) < 15:
        return False
    if t.rstrip().endswith(";"):
        return True
    m = _LO_FIRST_TOKEN_RE.match(t)
    if not m:
        return False
    first = m.group("first").lower()
    if first in _LO_SKIP_FIRST_WORD:
        return False
    return len(t) >= 25


def _split_page_into_blocks(page_text: str, page_num: int, start_idx: int) -> list[PDFBlock]:
    lines = [line.rstrip() for line in (page_text or "").splitlines()]
    blocks: list[PDFBlock] = []
    current: list[str] = []
    next_idx = start_idx

    def flush() -> None:
        nonlocal current, next_idx
        if not current:
            return
        text = normalize_ws(" ".join(current))
        current = []
        if not text:
            return
        blocks.append(
            PDFBlock(
                para_idx=next_idx,
                page_num=page_num,
                text=text,
                heading_level=infer_heading_level(text),
            )
        )
        next_idx += 1

    for line in lines:
        clean = line.strip()
        if not clean:
            flush()
            continue
        heading_like = infer_heading_level(clean) is not None
        list_like = bool(_BULLET_PREFIX_RE.match(clean) or _NUMBERED_LO_RE.match(clean))
        if heading_like or list_like:
            flush()
            text = normalize_ws(clean)
            if text:
                blocks.append(
                    PDFBlock(
                        para_idx=next_idx,
                        page_num=page_num,
                        text=text,
                        heading_level=infer_heading_level(text),
                    )
                )
                next_idx += 1
            continue
        current.append(clean)

    flush()
    return blocks


def _looks_like_image_caption_block(text: str) -> bool:
    text = normalize_ws(text)
    if not text:
        return False
    if len(text) > 180:
        return False
    return bool(_IMAGE_CAPTION_TRIGGER_RE.search(text))


def _select_image_anchor_indices(
    page_blocks: list[PDFBlock],
    image_count: int,
) -> list[int]:
    """Choose paragraph anchors for images on a PDF page.

    PDFs do not expose image-to-paragraph relationships like DOCX. We therefore
    approximate image placement by choosing paragraph indices from the page-local
    extracted text blocks:

    - prefer explicit caption-like blocks when they are abundant enough
    - otherwise prefer non-heading body blocks
    - otherwise fall back to all page blocks

    When a page contains multiple images, distribute anchors across the chosen
    blocks instead of assigning every image to the page's first paragraph.
    """
    if image_count <= 0:
        return []
    if not page_blocks:
        return [0] * image_count

    caption_blocks = [block for block in page_blocks if _looks_like_image_caption_block(block.text)]
    body_blocks = [block for block in page_blocks if block.heading_level is None]

    if len(caption_blocks) >= image_count:
        candidates = caption_blocks
    elif body_blocks:
        candidates = body_blocks
    else:
        candidates = page_blocks

    if not candidates:
        return [page_blocks[0].para_idx] * image_count

    if image_count == 1:
        return [candidates[len(candidates) // 2].para_idx]

    if len(candidates) == 1:
        return [candidates[0].para_idx] * image_count

    last_idx = len(candidates) - 1
    anchors: list[int] = []
    for image_pos in range(image_count):
        ratio = image_pos / (image_count - 1)
        candidate_idx = round(ratio * last_idx)
        anchors.append(candidates[candidate_idx].para_idx)
    return anchors


def _extract_pdf_outline(reader: PdfReader, source_label: str) -> list[TOCEntry]:
    outline_items = getattr(reader, "outline", None)
    if not outline_items:
        return []

    result: list[TOCEntry] = []

    def walk(items: list[Any], level: int = 1) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue

            title = _clean_toc_title(getattr(item, "title", "") or str(item))
            if not title or _is_structural_toc_title(title):
                continue

            page_number = None
            try:
                page_number = reader.get_destination_page_number(item) + 1
            except Exception:
                page_number = None

            result.append(
                TOCEntry(level=level, text=title, page=page_number, source=source_label)
            )

    try:
        walk(outline_items, 1)
    except Exception as exc:
        logger.warning("[pdf_parser] Could not read PDF outline: %s", exc)
        return []

    return result


class PDFSourceParser:
    """Extract A0-friendly metadata and content from one or more PDF files."""

    def __init__(self, pdf_paths: list[str]) -> None:
        if not pdf_paths:
            raise ValueError("At least one pdf path is required")
        self._pdf_paths = [Path(p) for p in pdf_paths]
        self._docs: list[PDFDocumentData] = []

        for path in self._pdf_paths:
            logger.info("[pdf_parser] Loading PDF source: %s", path.name)
            reader = PdfReader(str(path))
            blocks: list[PDFBlock] = []
            next_para_idx = 0
            for page_num, page in enumerate(reader.pages, start=1):
                raw_text = page.extract_text() or ""
                page_blocks = _split_page_into_blocks(raw_text, page_num, next_para_idx)
                blocks.extend(page_blocks)
                if page_blocks:
                    next_para_idx = page_blocks[-1].para_idx + 1
            raw_toc = _extract_pdf_outline(reader, path.name)
            toc_entries = _filter_toc_entries(raw_toc)
            self._docs.append(
                PDFDocumentData(path=path, reader=reader, blocks=blocks, toc_entries=toc_entries)
            )
            logger.info(
                "[pdf_parser] Parsed %s block(s), %s bookmark TOC (%s raw → %s kept) from %s",
                len(blocks),
                len(toc_entries),
                len(raw_toc),
                len(toc_entries),
                path.name,
            )
            if toc_entries:
                sample = toc_entries[:8]
                logger.info(
                    "[pdf_parser] TOC sample from %s: %s",
                    path.name,
                    " | ".join(f"L{e.level}:{e.text[:50]}" for e in sample),
                )

    def extract_title(self) -> str:
        for doc in self._docs:
            meta_title = ""
            try:
                meta_title = normalize_ws(getattr(doc.reader.metadata, "title", "") or "")
            except Exception:
                meta_title = ""
            if meta_title:
                return meta_title

            for block in doc.blocks[:20]:
                if block.heading_level is not None and len(block.text) >= 8:
                    return block.text

            for block in doc.blocks[:8]:
                if len(block.text) >= 8 and not _LO_TRIGGER_RE.match(block.text):
                    return block.text

        return self._pdf_paths[0].stem.replace("_", " ").replace("-", " ").strip() or "Course"

    def extract_course_id(self) -> str | None:
        for doc in self._docs:
            for block in doc.blocks[:10]:
                m = _COURSE_ID_RE.search(block.text)
                if m:
                    return m.group(1)
        return None

    def extract_learning_objectives(self, blocks: list[PDFBlock]) -> list[str]:
        max_skip = 3
        objectives: list[str] = []
        capture = False
        skip_count = 0

        for block in blocks:
            text = block.text.strip()
            low = text.lower()

            if any(
                phrase in low
                for phrase in ("learning objectives", "learning outcomes", "course objectives")
            ):
                capture = True
                skip_count = 0
                continue

            if not capture:
                continue

            if objectives and block.heading_level in (1, 2):
                break

            if not text:
                continue

            if _is_lo_intro_line(text, bool(objectives)):
                continue

            is_bullet_text = _has_leading_bullet_marker(text)
            is_numbered_text = bool(_NUMBERED_LO_RE.match(text))

            if is_numbered_text:
                clean = _NUMBERED_LO_RE.sub("", text).strip()
                objectives.append(clean.rstrip(";").rstrip("."))
                skip_count = 0
            elif is_bullet_text:
                clean = _strip_bullet_prefix(text)
                objectives.append(clean.rstrip(";").rstrip("."))
                skip_count = 0
            elif _looks_like_prose_objective(text):
                objectives.append(text.rstrip(";").rstrip("."))
                skip_count = 0
            else:
                skip_count += 1
                if skip_count > max_skip:
                    break

        return objectives

    def extract_merged_learning_objectives(self) -> list[str]:
        all_objectives: list[str] = []
        seen: set[str] = set()
        for doc in self._docs:
            for obj in self.extract_learning_objectives(doc.blocks):
                key = obj.lower()
                if key not in seen:
                    all_objectives.append(obj)
                    seen.add(key)
        return all_objectives

    def extract_content_sample(self, max_chars: int = 3000) -> str:
        parts: list[str] = []
        total = 0
        for doc in self._docs:
            if total >= max_chars:
                break
            header = f"\n--- Document: {doc.path.name} ---"
            parts.append(header)
            total += len(header)
            prev_was_heading = False
            for block in doc.blocks:
                if total >= max_chars:
                    break
                if block.heading_level is not None:
                    snippet = f"\n[Heading {block.heading_level}] {block.text}"
                    if total + len(snippet) > max_chars:
                        break
                    parts.append(snippet)
                    total += len(snippet)
                    prev_was_heading = True
                    continue
                if prev_was_heading:
                    snippet = block.text
                    remaining = max_chars - total
                    if remaining <= 0:
                        break
                    parts.append(snippet[:remaining])
                    total += min(len(snippet), remaining)
                    prev_was_heading = False
        return "\n".join(parts).strip()

    def extract_merged_full_content(self, max_words: int = 8000) -> str:
        parts: list[str] = []
        total_words = 0
        for doc in self._docs:
            if total_words >= max_words:
                break
            parts.append(f"\n--- Document: {doc.path.name} ---\n")
            for block in doc.blocks:
                if total_words >= max_words:
                    parts.append("[…content truncated at word limit…]")
                    break
                text = block.text.strip()
                if not text:
                    continue
                words = text.split()
                if total_words + len(words) >= max_words:
                    remaining = max_words - total_words
                    parts.append(" ".join(words[:remaining]))
                    parts.append("[…content truncated at word limit…]")
                    total_words = max_words
                    break
                parts.append(text)
                total_words += len(words)
        return "\n".join(parts)

    def extract_to_outline_text(self) -> str:
        """Extract PDF TO content into clean, structure-agnostic text for LLM parsing."""
        chunks: list[str] = []
        for doc in self._docs:
            chunks.append(f"--- Document: {doc.path.name} ---")
            for entry in doc.toc_entries:
                page = f" (page {entry.page})" if entry.page else ""
                chunks.append(f"[TOC L{entry.level}] {entry.text}{page}")
            for block in doc.blocks:
                prefix = f"[P{block.para_idx}]"
                if block.heading_level is not None:
                    chunks.append(f"{prefix} [Heading {block.heading_level}] {block.text}")
                else:
                    chunks.append(f"{prefix} {block.text}")
        text_output = "\n".join(chunks)
        text_output = re.sub(r"\n{3,}", "\n\n", text_output)
        return text_output.strip()

    def count_paragraphs(self) -> int:
        return sum(len(doc.blocks) for doc in self._docs)

    def count_total_doc_words(self) -> int:
        return sum(len(block.text.split()) for doc in self._docs for block in doc.blocks if block.text.strip())

    def get_section_heading_map(
        self,
    ) -> list[tuple[int, str, int] | tuple[int, str, int, str]]:
        multi = len(self._docs) > 1
        result: list[tuple[int, str, int] | tuple[int, str, int, str]] = []
        for doc in self._docs:
            for block in doc.blocks:
                if block.heading_level is None:
                    continue
                if multi:
                    result.append((block.para_idx, block.text, block.heading_level, doc.path.name))
                else:
                    result.append((block.para_idx, block.text, block.heading_level))
        return result

    def paragraphs_by_source(self) -> dict[str, int]:
        return {doc.path.name: len(doc.blocks) for doc in self._docs}

    def _build_block_anchors(
        self,
    ) -> tuple[list[tuple[int, str, PDFDocumentData, int]], list[str]]:
        anchors: list[tuple[int, str, PDFDocumentData, int]] = []
        for doc in self._docs:
            for block in doc.blocks:
                block_text = normalize_ws(block.text)
                if not block_text:
                    continue
                anchors.append((block.para_idx, block_text, doc, block.page_num))
        norm_anchors = [_norm_heading(anchor[1]) for anchor in anchors]
        return anchors, norm_anchors

    def _best_anchor_for_entry(
        self,
        entry: TOCEntry,
        anchors: list[tuple[int, str, PDFDocumentData, int]],
        norm_anchors: list[str],
    ) -> tuple[int, str, PDFDocumentData, int] | None:
        if not anchors:
            return None
        key = _norm_heading(entry.text)
        if not key:
            return None
        best: tuple[int, tuple[int, str, PDFDocumentData, int]] | None = None
        for idx, anchor in enumerate(anchors):
            para_idx, block_text, doc, block_page = anchor
            score = 0
            if entry.source and doc.path.name == entry.source:
                score += 3
            if entry.page and block_page == entry.page:
                score += 2
            if norm_anchors[idx] == key:
                score += 6
            elif key and (key in norm_anchors[idx] or norm_anchors[idx] in key):
                score += 3
            elif key and key in _norm_heading(block_text):
                score += 2
            if best is None or score > best[0]:
                best = (score, anchor)
        return best[1] if best and best[0] > 0 else None

    def extract_merged_heading_tree(self) -> list[dict[str, Any]]:
        """Prefer PDF bookmark TOC (clean, hierarchical); fall back to body heuristics."""
        tree: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        bookmark_entries: list[TOCEntry] = []
        for doc in self._docs:
            bookmark_entries.extend(doc.toc_entries)

        if bookmark_entries:
            anchors, norm_anchors = self._build_block_anchors()
            for entry in bookmark_entries:
                anchor = self._best_anchor_for_entry(entry, anchors, norm_anchors)
                para_idx = anchor[0] if anchor else -1
                source = entry.source or (anchor[2].path.name if anchor else "")
                key = (source, entry.text.lower())
                if key in seen:
                    continue
                tree.append(
                    {
                        "level": entry.level,
                        "text": entry.text,
                        "para_idx": para_idx,
                        "source": source,
                    }
                )
                seen.add(key)
            logger.info(
                "[pdf_parser] heading_tree from PDF bookmarks: %d entries "
                "(%d with body para_idx)",
                len(tree),
                sum(1 for h in tree if int(h.get("para_idx", -1)) >= 0),
            )
            return tree

        for doc in self._docs:
            for block in doc.blocks:
                if block.heading_level is None:
                    continue
                if not _is_plausible_body_heading(block.text):
                    continue
                key = (doc.path.name, block.text.lower())
                if key in seen:
                    continue
                tree.append(
                    {
                        "level": block.heading_level,
                        "text": block.text,
                        "para_idx": block.para_idx,
                        "source": doc.path.name,
                    }
                )
                seen.add(key)
        logger.info(
            "[pdf_parser] heading_tree from body heuristics (no bookmarks): %d entries",
            len(tree),
        )
        return tree

    def _extract_heading_based_toc_entries(self) -> list[TOCEntry]:
        """Build TOC-like entries from inferred body headings.

        This is a generic fallback for PDFs that do not expose bookmarks but
        still have recognizable numbered or all-caps headings in the extracted
        page text.
        """
        entries: list[TOCEntry] = []
        seen: set[tuple[str, int, int, str]] = set()

        for doc in self._docs:
            for block in doc.blocks:
                if block.heading_level is None:
                    continue
                if not _is_plausible_body_heading(block.text):
                    continue

                key = (
                    _norm_heading(block.text),
                    block.heading_level,
                    block.page_num,
                    doc.path.name,
                )
                if not key[0] or key in seen:
                    continue

                entries.append(
                    TOCEntry(
                        level=block.heading_level,
                        text=block.text,
                        page=block.page_num,
                        source=doc.path.name,
                    )
                )
                seen.add(key)

        return _filter_toc_entries(entries)

    def extract_indexed_content(self, max_words: int | None = 8000) -> str:
        lines: list[str] = []
        total_words = 0
        for doc in self._docs:
            if max_words is not None and total_words >= max_words:
                break
            lines.append(f"\n--- Document: {doc.path.name} ---")
            for block in doc.blocks:
                words = block.text.split()
                if not words:
                    continue
                if max_words is not None and total_words + len(words) > max_words:
                    remaining = max_words - total_words
                    if remaining > 0:
                        lines.append(f"[P{block.para_idx}] " + " ".join(words[:remaining]))
                    lines.append("[…content truncated at word limit…]")
                    total_words = max_words
                    break
                lines.append(f"[P{block.para_idx}] {block.text}")
                total_words += len(words)
        return "\n".join(lines)

    def extract_toc_entries(self, *, include_heading_fallback: bool = False) -> list[TOCEntry]:
        """Return TOC entries from PDF outlines, with optional heading fallback.

        By default this preserves the existing behavior of preferring embedded
        PDF bookmark/outline entries only. When ``include_heading_fallback`` is
        enabled, the parser synthesizes TOC entries from inferred body headings
        if the PDF does not expose bookmarks.
        """
        outline_entries: list[TOCEntry] = []
        for doc in self._docs:
            outline_entries.extend(doc.toc_entries)

        if outline_entries:
            logger.info(
                "[pdf_parser] TOC source=bookmarks (%d entries across %d PDF(s))",
                len(outline_entries),
                len(self._docs),
            )
            return outline_entries
        if include_heading_fallback:
            fallback = self._extract_heading_based_toc_entries()
            logger.info(
                "[pdf_parser] TOC source=body-heuristic fallback (%d entries)",
                len(fallback),
            )
            return fallback
        logger.info("[pdf_parser] TOC source=none (no bookmarks or fallback)")
        return []

    def extract_toc_hierarchy(
        self,
        *,
        include_heading_fallback: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a nested topic/sub-topic tree derived from TOC entries."""
        return toc_entries_to_hierarchy(
            self.extract_toc_entries(include_heading_fallback=include_heading_fallback)
        )

    def extract_toc_section_contents(
        self,
        toc_entries: list[TOCEntry],
        total_word_budget: int = 8000,
    ) -> list[dict[str, Any]]:
        if not toc_entries:
            return []

        # Respect the caller's total prompt budget across all TOC sections.
        n = max(len(toc_entries), 1)
        per_section_words = max(1, min(1500, total_word_budget // n))

        anchors, norm_anchors = self._build_block_anchors()

        resolved: list[tuple[int, str, PDFDocumentData, int] | None] = [
            self._best_anchor_for_entry(entry, anchors, norm_anchors)
            for entry in toc_entries
        ]
        matched = sum(1 for anchor in resolved if anchor is not None)
        logger.info(
            "[pdf_parser] TOC section content: %d entries, %d anchored to body text",
            len(toc_entries),
            matched,
        )

        result: list[dict[str, Any]] = []
        for i, (entry, anchor) in enumerate(zip(toc_entries, resolved)):
            if anchor is None:
                result.append(
                    {
                        "level": entry.level,
                        "title": entry.text,
                        "para_idx_start": None,
                        "para_idx_end": None,
                        "source": entry.source,
                        "indexed_content": "",
                    }
                )
                continue

            start_idx, _anchor_text, src_doc, _anchor_page = anchor
            end_idx: int | None = None
            for j in range(i + 1, len(toc_entries)):
                next_anchor = resolved[j]
                if next_anchor is None:
                    continue
                next_start, _, next_doc, _next_page = next_anchor
                if next_doc.path != src_doc.path:
                    break
                if toc_entries[j].level <= entry.level:
                    end_idx = next_start - 1
                    break
            if end_idx is None:
                end_idx = len(src_doc.blocks) - 1

            content_lines: list[str] = []
            words_used = 0
            for block in src_doc.blocks:
                if block.para_idx < start_idx or block.para_idx > end_idx:
                    continue
                words = block.text.split()
                if words_used + len(words) > per_section_words:
                    remaining = per_section_words - words_used
                    if remaining > 0:
                        content_lines.append(f"[P{block.para_idx}] " + " ".join(words[:remaining]))
                    content_lines.append("[…section truncated…]")
                    break
                content_lines.append(f"[P{block.para_idx}] {block.text}")
                words_used += len(words)

            result.append(
                {
                    "level": entry.level,
                    "title": entry.text,
                    "para_idx_start": start_idx,
                    "para_idx_end": end_idx,
                    "source": entry.source or src_doc.path.name,
                    "indexed_content": "\n".join(content_lines),
                }
            )

        return result

    def _safe_image_basename(self, name: str, default: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
        return clean or default

    def _guess_image_ext(self, image_obj: Any) -> str:
        raw_name = str(getattr(image_obj, "name", "") or "")
        suffix = Path(raw_name).suffix.lower()
        if suffix:
            return suffix
        try:
            fmt = (getattr(image_obj.image, "format", "") or "").lower()
            if fmt in {"jpeg", "jpg"}:
                return ".jpg"
            if fmt:
                return f".{fmt}"
        except Exception:
            pass
        return ".bin"

    def _pixel_to_cm(self, px: int, dpi: float | None) -> float | None:
        if not dpi or dpi <= 0:
            return None
        return round((px / dpi) * 2.54, 1)

    def _heading_anchor_for_idx(
        self,
        heuristic_headings: list[dict[str, Any]],
        para_idx: int,
    ) -> tuple[int, str, int] | None:
        """Return the active heading span anchor at ``para_idx`` when available."""
        heading_anchor: tuple[int, str, int] | None = None
        for heading in heuristic_headings:
            if heading["para_idx"] <= para_idx:
                heading_anchor = (
                    int(heading["para_idx"]),
                    str(heading["text"]),
                    int(heading["level"]),
                )
            else:
                break
        return heading_anchor

    def _heading_context_for_idx(self, heuristic_headings: list[dict[str, Any]], para_idx: int) -> tuple[str, int]:
        heading_anchor = self._heading_anchor_for_idx(heuristic_headings, para_idx)
        if heading_anchor is None:
            return ("", 0)
        return (heading_anchor[1], heading_anchor[2])

    def _infer_page_caption(self, page_blocks: list[PDFBlock], image_count: int) -> str:
        if image_count != 1:
            return ""
        for idx, block in enumerate(page_blocks):
            text = block.text
            if _IMAGE_CAPTION_TRIGGER_RE.search(text):
                return text
            if idx > 0 and _IMAGE_CAPTION_TRIGGER_RE.search(page_blocks[idx - 1].text):
                return page_blocks[idx - 1].text
        for block in page_blocks:
            if len(block.text) <= 120 and not block.text.endswith("."):
                return block.text
        return ""

    def extract_all_images(
        self,
        images_dir: Path,
        *,
        start_seq: int = 0,
        heading_anchors: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        images_dir.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []
        img_seq = start_seq

        for doc in self._docs:
            heuristic_headings = [
                heading
                for heading in (
                    heading_anchors
                    if heading_anchors is not None
                    else self.extract_merged_heading_tree()
                )
                if heading.get("source") == doc.path.name
            ]
            heuristic_headings.sort(key=lambda heading: int(heading.get("para_idx", -1)))
            for page_num, page in enumerate(doc.reader.pages, start=1):
                try:
                    page_images = list(page.images)
                except Exception:
                    page_images = []
                if not page_images:
                    continue

                page_blocks = [block for block in doc.blocks if block.page_num == page_num]
                caption = self._infer_page_caption(page_blocks, len(page_images))
                anchor_indices = _select_image_anchor_indices(page_blocks, len(page_images))

                for image_idx, image_obj in enumerate(page_images):
                    img_seq += 1
                    img_id = f"img_{img_seq:03d}"
                    ext = self._guess_image_ext(image_obj)
                    base_name = self._safe_image_basename(
                        Path(str(getattr(image_obj, "name", "") or f"page_{page_num}_{img_id}")).stem,
                        f"page_{page_num}_{img_id}",
                    )
                    media_filename = f"{base_name}{ext}"
                    save_name = f"{img_id}_{media_filename}"
                    save_path = images_dir / save_name
                    img_bytes = image_obj.data
                    save_path.write_bytes(img_bytes)
                    content_anchor_para_idx = (
                        anchor_indices[image_idx]
                        if image_idx < len(anchor_indices)
                        else (page_blocks[0].para_idx if page_blocks else 0)
                    )
                    heading_anchor = self._heading_anchor_for_idx(
                        heuristic_headings,
                        content_anchor_para_idx,
                    )
                    if heading_anchor is not None:
                        image_para_idx, heading_context, heading_level = heading_anchor
                    else:
                        image_para_idx = content_anchor_para_idx
                        heading_context = ""
                        heading_level = 0

                    width_cm = None
                    height_cm = None
                    try:
                        pil_image = image_obj.image
                        width_px, height_px = pil_image.size
                        dpi_info = getattr(pil_image, "info", {}).get("dpi")
                        dpi = None
                        if isinstance(dpi_info, tuple) and dpi_info and dpi_info[0]:
                            dpi = float(dpi_info[0])
                        elif isinstance(dpi_info, (int, float)):
                            dpi = float(dpi_info)
                        width_cm = self._pixel_to_cm(width_px, dpi)
                        height_cm = self._pixel_to_cm(height_px, dpi)
                    except Exception:
                        pass

                    images.append(
                        {
                            "id": img_id,
                            "r_embed": str(getattr(image_obj, "name", "") or base_name),
                            "media_filename": media_filename,
                            "saved_path": str(save_path),
                            # PDF images are owned by the active heading span:
                            # from this heading through the block before the next heading.
                            "para_idx": image_para_idx,
                            "content_para_idx": content_anchor_para_idx,
                            "size_cm": {"width": width_cm, "height": height_cm},
                            "size_bytes": len(img_bytes),
                            "sha256": hashlib.sha256(img_bytes).hexdigest()[:16],
                            "caption": caption,
                            "has_caption": bool(caption),
                            "alt_text": "",
                            "heading_context": heading_context,
                            "heading_level": heading_level,
                            "source_document": doc.path.name,
                            "source_page": page_num,
                        }
                    )

        return images
