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


def extract_last_section_tail(completed_sections: list[dict], max_words: int = 80) -> str:
    """
    Return a compact summary of the most recently generated section for bridging.

    Captures three things in priority order:
    1. Any important_callout blocks — these are explicit key takeaways.
    2. The closing prose (last paragraph) — the section's final teaching point.
    3. Up to two bullet items if prose is absent.

    The combined result is trimmed to max_words so the bridge stays lightweight.
    Used to give the LLM enough context to open the next lesson with a natural
    single-sentence transition rather than starting cold.

    Returns an empty string when no suitable content is found (first lesson,
    failed sections, etc.).
    """
    for sec in reversed(completed_sections):
        if sec.get("status") == "failed":
            continue
        heading = sec.get("heading", "")
        paragraphs = sec.get("body_paragraphs") or []

        callouts: list[str] = []
        prose: list[str] = []
        bullets: list[str] = []

        for para in paragraphs:
            ptype = para.get("type", "")
            if ptype == "important_callout":
                content = (para.get("content") or "").strip()
                if content:
                    label = (para.get("label") or "").strip()
                    callouts.append(f"{label}: {content}" if label else content)
            elif ptype in ("text", "heading_3", "heading_4"):
                content = (para.get("content") or "").strip()
                if content:
                    prose.append(content)
            elif ptype in ("bullet_list", "numbered_list"):
                for item in (para.get("items") or [])[:2]:
                    bullets.append(str(item).strip())

        if not callouts and not prose and not bullets:
            continue

        parts: list[str] = []
        # Include the most recent callout (highest instructional signal)
        if callouts:
            parts.append(callouts[-1])
        # Include the final prose paragraph
        if prose:
            parts.append(prose[-1])
        # Fallback to bullets when prose is absent
        if not prose and bullets:
            parts.append("; ".join(bullets[:2]))

        combined = " | ".join(p for p in parts if p)
        words = combined.split()
        tail = " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
        return f'Section: "{heading}"\nKey takeaway: {tail}'
    return ""


def extract_section_key_points(section: dict, max_words: int = 50) -> str:
    """
    Return a concise summary of key points from a single generated section.

    Prefers callout labels and content, then the first text paragraph.
    Used to populate `prev_section_key_points` in subtopic_specs for
    intra-lesson section-to-section bridging.

    Returns an empty string when the section has no usable body content.
    """
    paragraphs = section.get("body_paragraphs") or []
    heading = (section.get("heading") or "").strip()

    callout_text = ""
    first_prose = ""

    for para in paragraphs:
        ptype = para.get("type", "")
        if ptype == "important_callout" and not callout_text:
            label = (para.get("label") or "").strip()
            content = (para.get("content") or "").strip()
            callout_text = f"{label}: {content}" if label else content
        elif ptype == "text" and not first_prose:
            first_prose = (para.get("content") or "").strip()

    raw = callout_text or first_prose
    if not raw:
        return heading  # bare heading is better than nothing

    words = raw.split()
    summary = " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
    return summary
