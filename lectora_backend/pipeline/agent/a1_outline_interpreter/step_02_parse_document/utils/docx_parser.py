"""Step 02 — DOCX section parser using python-docx."""
import logging

from docx import Document
from ...shared.utils.text_utils import to_snake
from .pdf_parser import _append_section_body

logger = logging.getLogger(__name__)


def parse_docx_document(docx_path: str) -> tuple[list[dict], int, int]:
    """Parse a DOCX into sections. Returns (sections, total_words, kc_count)."""
    doc = Document(docx_path)
    all_paras = doc.paragraphs
    sections: list[dict] = []
    current: dict | None = None
    kc_count = 0

    for para_idx, p in enumerate(all_paras):
        style = p.style.name
        text = p.text.strip()
        if not text:
            continue

        if style in ("Heading 1", "Heading 2", "Heading 3"):
            level = int(style[-1])
            is_kc = "Knowledge Check" in text and level == 3

            if is_kc and current is not None:
                current["has_knowledge_check"] = True
                kc_count += 1
                _append_section_body(current, text)
                continue

            if current is not None:
                current["para_end"] = para_idx - 1
                sections.append(current)

            current = {
                "id": f"s{len(sections)+1}_{to_snake(text)}",
                "heading": text,
                "level": level,
                "is_knowledge_check": False,
                "has_knowledge_check": False,
                "para_start": para_idx,
                "para_end": para_idx,
                "paragraphs": [],
                "word_count": 0,
                "interactive_elements": [],
            }
        elif current is not None:
            _append_section_body(current, text)

    if current is not None:
        current["para_end"] = len(all_paras) - 1
        sections.append(current)

    total_words = sum(s["word_count"] for s in sections)
    return sections, total_words, kc_count
