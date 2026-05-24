"""
LLM classifier and value resolution utilities for A0.

Azure OpenAI client/model settings live in config/llm.py.
This module only contains business logic.
"""

import difflib
import json
import re
from typing import Any

from ..config.llm import chat
from ..prompt.classification import (
    CLASSIFICATION_PROMPT,
    CLASSIFICATIONTO_OUTLINE_PROMPT,
    GENERATE_TO_PROMPT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove markdown code fences that some models insert."""
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def classify_with_llm(
    title: str,
    objectives: list[str],
    content_sample: str,
    *,
    validation_hints: str | None = None,
) -> dict:
    """Classify the course into a rule family and infer metadata via AzureOpenAI."""
    user_msg = (
        f"## Course Title\n{title}\n\n"
        f"## Learning Objectives\n"
        + "\n".join(f"- {obj}" for obj in objectives)
        + f"\n\n## Content Sample\n{content_sample}"
    )
    if validation_hints:
        user_msg += (
            "\n\n## Prior S1 validation feedback (resolve inconsistencies with this run)\n"
            + validation_hints.strip()
        )

    raw = chat(CLASSIFICATION_PROMPT, user_msg)
    try:
        return json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned invalid JSON for course classification. "
            f"Raw output (first 500 chars): {raw[:500]!r}"
        ) from exc


def resolve_value(
    key: str, explicit: dict, rule_defaults: dict, inferred: dict
) -> tuple[Any, str]:
    """
    Resolve a value from three sources in priority order.

    Returns (value, source) where source is one of:
      'explicitly_provided', 'derived_from_rule_pack', 'inferred'
    """
    if key in explicit and explicit[key] is not None:
        return explicit[key], "explicitly_provided"
    if key in rule_defaults and rule_defaults[key] is not None:
        return rule_defaults[key], "derived_from_rule_pack"
    if key in inferred and inferred[key] is not None:
        return inferred[key], "inferred"
    return None, "unresolved"



def _format_heading_tree(heading_tree: list[dict]) -> str:
    """Format a heading tree list as a readable outline string for the LLM prompt."""
    lines: list[str] = []
    for h in heading_tree:
        indent = "  " * (h.get("level", 1) - 1)
        source = h.get("source", "")
        source_tag = f" [{source}]" if source else ""
        lines.append(f"{indent}[L{h.get('level', 1)}] {h.get('text', '')}{source_tag}")
    return "\n".join(lines)


def generate_to_with_llm(
    title: str,
    objectives: list[str],
    indexed_content: str,
    *,
    course_difficulty: str = "intermediate",
    validation_hints: str | None = None,
    custom_system_prompt: str | None = None,
    heading_tree: list[dict] | None = None,
    course_type_hint: str | None = None,
) -> dict:
    """Generate a structured Timed Outline from indexed source document content.

    Used when no TO document is provided (Scenario 2). The indexed_content must
    be produced by CourseDocParser.extract_indexed_content() so that each paragraph
    is prefixed with [P<N>], allowing the LLM to set para_idx_start / para_idx_end
    on each generated section.

    If ``custom_system_prompt`` is provided it replaces ``GENERATE_TO_PROMPT`` as
    the system message, allowing callers to guide TO generation without changing
    the codebase. The expected JSON output schema must still be honoured.

    ``heading_tree`` — structured headings from all uploaded files; when present,
    the LLM uses them as the primary structural skeleton for the TO.

    ``course_type_hint`` — optional domain context (e.g. "Washington LTC Compliance
    Course") used to filter and prioritize topics.
    """
    user_msg = f"## Course Difficulty\n{course_difficulty}\n\n"

    if course_type_hint and course_type_hint.strip():
        user_msg += f"## Course Type Context\n{course_type_hint.strip()}\n\n"

    user_msg += (
        f"## Course Title\n{title}\n\n"
        f"## Learning Objectives\n"
        + "\n".join(f"- {obj}" for obj in objectives)
    )

    if heading_tree:
        user_msg += f"\n\n## Document Heading Structure (from all uploaded files)\n{_format_heading_tree(heading_tree)}"

    user_msg += f"\n\n## Source Document Content (with paragraph indices)\n{indexed_content}"

    if validation_hints:
        user_msg += (
            "\n\n## Prior validation feedback (resolve these issues in the generated outline)\n"
            + validation_hints.strip()
        )

    system_prompt = custom_system_prompt.strip() if custom_system_prompt else GENERATE_TO_PROMPT
    raw = chat(system_prompt, user_msg)
    cleaned = _strip_fences(raw)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned invalid JSON for TO generation. "
            f"Raw output (first 500 chars): {raw[:500]!r}"
        ) from exc


def map_to_to_source_indices(
    sections: list[dict],
    heading_map: list[tuple],
    total_paragraphs: int,
    *,
    paragraphs_by_source: dict[str, int] | None = None,
) -> list[dict]:
    """Add para_idx_start / para_idx_end to TO sections parsed from a TO document.

    Used in Scenario 1 (TO provided). Maps each section title to the nearest
    matching heading in the source document using fuzzy title matching, then
    assigns paragraph ranges so downstream agents can fetch raw source content.

    Args:
        sections:         List of section dicts from the parsed TO.
        heading_map:      Output of CourseDocParser.get_section_heading_map() —
                          (para_idx, heading_text, heading_level) or with a 4th
                          element: source filename when multiple DOCX files are loaded.
        total_paragraphs: Fallback paragraph count for end-of-doc sections.
        paragraphs_by_source: Optional map of filename -> paragraph count per file.

    Returns:
        Same list with para_idx_start and para_idx_end set on each section dict.
    """
    if not heading_map:
        for section in sections:
            section.setdefault("para_idx_start", None)
            section.setdefault("para_idx_end", None)
        return sections

    heading_para_indices: list[int] = []
    heading_texts: list[str] = []
    heading_sources: list[str | None] = []
    for h in heading_map:
        heading_para_indices.append(h[0])
        heading_texts.append(h[1])
        heading_sources.append(h[3] if len(h) > 3 else None)

    _NUMBER_PREFIX_RE = re.compile(r"^\d+(\.\d+)*[\s.\-:]*")

    def _clean(title: str) -> str:
        return _NUMBER_PREFIX_RE.sub("", title).lower().strip()

    def _best_match_heading_pos(section_title: str) -> int | None:
        clean = _clean(section_title)
        if not clean:
            return None
        cleaned_headings = [_clean(h) for h in heading_texts]
        matches = difflib.get_close_matches(clean, cleaned_headings, n=1, cutoff=0.4)
        if matches:
            return cleaned_headings.index(matches[0])
        return None

    result: list[dict] = []
    for i, section in enumerate(sections):
        sec = dict(section)
        pos = _best_match_heading_pos(sec.get("title", ""))
        if pos is not None:
            start = heading_para_indices[pos]
            source_file = heading_sources[pos]
            sec["para_idx_start"] = start
            if source_file:
                sec["source_document"] = source_file
            if i + 1 < len(sections):
                next_pos = _best_match_heading_pos(sections[i + 1].get("title", ""))
                next_start = heading_para_indices[next_pos] if next_pos is not None else None
                sec["para_idx_end"] = (next_start - 1) if (next_start and next_start > start) else None
            else:
                end_total = total_paragraphs - 1
                if source_file and paragraphs_by_source:
                    end_total = paragraphs_by_source.get(source_file, total_paragraphs) - 1
                sec["para_idx_end"] = end_total
        else:
            sec["para_idx_start"] = None
            sec["para_idx_end"] = None
        result.append(sec)

    return result


def classify_to_outline_with_llm(
    content_sample: str,
    *,
    validation_hints: str | None = None,
) -> dict:
    """Parse raw TO content into the structured outline format via AzureOpenAI."""
    user_msg = f"## Content\n{content_sample}"
    if validation_hints:
        user_msg += (
            "\n\n## Prior S1 validation feedback (align outline structure accordingly)\n"
            + validation_hints.strip()
        )

    raw = chat(CLASSIFICATIONTO_OUTLINE_PROMPT, user_msg)
    cleaned = _strip_fences(raw)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned invalid JSON for timed-outline classification. "
            f"Raw output (first 500 chars): {raw[:500]!r}"
        ) from exc
