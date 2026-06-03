"""
Source chunker — extracts relevant paragraphs from the original .docx
for a given section, based on paragraph index ranges from course_spec.

Each LLM call receives the FULL paragraph text for that section's para_start
to para_end range — no truncation.
"""

import json
import re

from docx import Document


def count_tokens_approx(text: str) -> int:
    """Rough token estimate: ~0.75 words per token for English."""
    return int(len(re.findall(r"\w+", text)) / 0.75)


def _load_paragraphs_from_indexed_content(indexed_content: str) -> list[str]:
    """
    Convert ``[P<N>] text`` blocks from A0's indexed_content into an ordered
    list of paragraph strings (index → position in list).

    Gaps in the ``[P<N>]`` sequence are filled with empty strings so that
    ``doc_paragraphs[para_idx]`` gives the correct block for any para_idx
    stored in the section map.
    """
    pairs: list[tuple[int, str]] = []
    for m in re.finditer(r"\[P(\d+)\]\s*(.*?)(?=\[P\d+\]|\Z)", indexed_content, re.DOTALL):
        idx = int(m.group(1))
        text = m.group(2).strip()
        pairs.append((idx, text))

    if not pairs:
        return []

    max_idx = max(i for i, _ in pairs)
    result: list[str] = [""] * (max_idx + 1)
    for idx, text in pairs:
        result[idx] = text
    return result


def load_doc_paragraphs(
    docx_path: str,
    shared_state_path: str | None = None,
) -> list[str]:
    """
    Load and return all paragraph texts (by index) from a source document.

    For **DOCX** files: opens the file with python-docx and returns stripped
    paragraph texts in document order.

    For **PDF** files: paragraphs are reconstructed from the ``[P<N>]``-
    annotated ``indexed_content`` field in ``shared_state.json``.  The
    ``shared_state_path`` argument must be provided for the PDF path to work;
    if it is absent or the field is empty the function returns ``[]``.

    Call once per pipeline run and pass the result to the helpers below
    to avoid repeated disk reads.
    """
    if docx_path.lower().endswith(".pdf"):
        if not shared_state_path:
            return []
        try:
            with open(shared_state_path) as fh:
                state = json.load(fh)
            indexed_content: str = (
                state.get("extracted_inputs", {}).get("indexed_content", "") or ""
            )
            return _load_paragraphs_from_indexed_content(indexed_content)
        except Exception:
            return []

    doc = Document(docx_path)
    return [p.text.strip() for p in doc.paragraphs]


def count_source_words(
    doc_paragraphs: list[str],
    para_start: int,
    para_end: int,
) -> int:
    """
    Count words in paragraphs [para_start..para_end] without any truncation.
    Used to measure the real source content length for proportional word-count
    distribution across subtopics.
    """
    start = max(0, para_start)
    end   = min(len(doc_paragraphs) - 1, para_end)
    total = 0
    for i in range(start, end + 1):
        total += len(re.findall(r"\w+", doc_paragraphs[i]))
    return total


def extract_full_section_text(
    doc_paragraphs: list[str],
    para_start: int,
    para_end: int,
) -> str:
    """
    Return the COMPLETE text for paragraphs [para_start..para_end].
    No truncation — every paragraph is included so the LLM can analyze
    the full source content for this section.
    """
    start = max(0, para_start)
    end   = min(len(doc_paragraphs) - 1, para_end)
    lines = [doc_paragraphs[i] for i in range(start, end + 1) if doc_paragraphs[i]]
    return "\n\n".join(lines)


def build_prior_summary(completed_sections: list[dict], max_chars: int = 600) -> str:
    """
    Build a brief summary of previously completed sections.
    Only includes heading + subtopics + word count — never full text.
    This keeps the LLM context lightweight.
    """
    if not completed_sections:
        return "This is the first section of the course."

    parts = []
    total_chars = 0
    for sec in completed_sections:
        heading = sec.get("heading", "")
        subtopics = ", ".join(sec.get("subtopics", [])[:4])
        wc = sec.get("word_count", 0)
        line = f"- {heading} ({wc}w): {subtopics}"
        if total_chars + len(line) > max_chars:
            parts.append(f"- ... and {len(completed_sections) - len(parts)} more sections")
            break
        parts.append(line)
        total_chars += len(line)

    return "Previously completed sections:\n" + "\n".join(parts)
