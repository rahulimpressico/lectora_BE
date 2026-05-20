"""
Source chunker — extracts relevant paragraphs from the original .docx
for a given section, based on paragraph index ranges from course_spec.

Each LLM call receives the FULL paragraph text for that section's para_start
to para_end range — no truncation.
"""

import re

from docx import Document


def count_tokens_approx(text: str) -> int:
    """Rough token estimate: ~0.75 words per token for English."""
    return int(len(re.findall(r"\w+", text)) / 0.75)


def load_doc_paragraphs(docx_path: str) -> list[str]:
    """
    Load and return all non-empty paragraph texts from a .docx file.
    Call once per pipeline run and pass the result to the helpers below
    to avoid repeated disk reads.
    """
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
