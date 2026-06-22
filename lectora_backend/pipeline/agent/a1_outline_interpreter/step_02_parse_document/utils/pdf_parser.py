"""Step 02 — PDF section parser: reconstructs sections from A0 shared-state data."""
import logging
import re as _re

from ...shared.helpers.section_helpers import _normalize_section_level
from ...shared.utils.text_utils import count_words, to_snake
from lectora_backend.pipeline.shared_utils.interactive_elements import collect_interactive_elements

logger = logging.getLogger(__name__)


def _append_section_body(current: dict, text: str) -> None:
    """Add a non-heading paragraph to the open section."""
    current["paragraphs"].append(text)
    current["word_count"] += count_words(text)
    current["interactive_elements"] = collect_interactive_elements(
        [text],
        initial=current.get("interactive_elements", []),
    )


def _parse_pdf_sections_from_shared_state(a0_data: dict) -> tuple[list[dict], int, int]:
    """Reconstruct raw_sections from A0's heading_tree + indexed_content."""
    extracted: dict = a0_data.get("extracted_inputs", {})
    heading_tree: list[dict] = extracted.get("heading_tree", [])
    indexed_content: str = extracted.get("indexed_content", "") or ""

    para_map: dict[int, str] = {}
    for m in _re.finditer(r"\[P(\d+)\]\s*(.*?)(?=\[P\d+\]|\Z)", indexed_content, _re.DOTALL):
        idx = int(m.group(1))
        text = m.group(2).strip()
        if text:
            para_map[idx] = text

    max_para = max(para_map.keys(), default=0)

    if not heading_tree:
        all_paras = [para_map[k] for k in sorted(para_map.keys())]
        all_text = " ".join(all_paras)
        wc = count_words(all_text)
        interactive_elements = collect_interactive_elements(all_paras)
        return (
            [{
                "id": "s1_content",
                "heading": "Content",
                "level": 1,
                "is_knowledge_check": False,
                "has_knowledge_check": False,
                "para_start": 0,
                "para_end": max_para,
                "paragraphs": all_paras,
                "word_count": wc,
                "interactive_elements": interactive_elements,
            }],
            wc,
            0,
        )

    sections: list[dict] = []
    kc_count = 0

    for i, h in enumerate(heading_tree):
        para_start: int = h.get("para_idx", 0)
        para_end: int = (
            heading_tree[i + 1].get("para_idx", para_start) - 1
            if i + 1 < len(heading_tree)
            else max_para
        )
        level: int = _normalize_section_level(h.get("level", 1))
        heading_text: str = h.get("text", "")
        is_kc = "Knowledge Check" in heading_text and level == 3

        body_paras = [
            para_map[j]
            for j in range(para_start + 1, para_end + 1)
            if j in para_map
        ]
        wc = count_words(" ".join(body_paras))
        interactive_elements = collect_interactive_elements(body_paras)

        section: dict = {
            "id": f"s{len(sections)+1}_{to_snake(heading_text)}",
            "heading": heading_text,
            "level": level,
            "is_knowledge_check": is_kc,
            "has_knowledge_check": False,
            "para_start": para_start,
            "para_end": para_end,
            "paragraphs": body_paras,
            "word_count": wc,
            "interactive_elements": interactive_elements,
        }

        if is_kc and sections:
            sections[-1]["has_knowledge_check"] = True
            kc_count += 1

        sections.append(section)

    total_words = sum(s["word_count"] for s in sections)
    return sections, total_words, kc_count
