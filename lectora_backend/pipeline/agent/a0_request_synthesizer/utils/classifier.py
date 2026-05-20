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



def generate_to_with_llm(
    title: str,
    objectives: list[str],
    indexed_content: str,
    *,
    course_difficulty: str = "intermediate",
    validation_hints: str | None = None,
) -> dict:
    """Generate a structured Timed Outline from indexed source document content.

    Used when no TO document is provided (Scenario 2). The indexed_content must
    be produced by CourseDocParser.extract_indexed_content() so that each paragraph
    is prefixed with [P<N>], allowing the LLM to set para_idx_start / para_idx_end
    on each generated section.
    """
    user_msg = (
        f"## Course Difficulty\n{course_difficulty}\n\n"
        f"## Course Title\n{title}\n\n"
        f"## Learning Objectives\n"
        + "\n".join(f"- {obj}" for obj in objectives)
        + f"\n\n## Source Document Content (with paragraph indices)\n{indexed_content}"
    )
    if validation_hints:
        user_msg += (
            "\n\n## Prior validation feedback (resolve these issues in the generated outline)\n"
            + validation_hints.strip()
        )

    raw = chat(GENERATE_TO_PROMPT, user_msg)
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
    heading_map: list[tuple[int, str, int]],
    total_paragraphs: int,
) -> list[dict]:
    """Add para_idx_start / para_idx_end to TO sections parsed from a TO document.

    Used in Scenario 1 (TO provided). Maps each section title to the nearest
    matching heading in the source document using fuzzy title matching, then
    assigns paragraph ranges so downstream agents can fetch raw source content.

    Args:
        sections:         List of section dicts from the parsed TO.
        heading_map:      Output of CourseDocParser.get_section_heading_map() —
                          list of (para_idx, heading_text, heading_level).
        total_paragraphs: Total paragraph count in the source doc (for end-of-doc sections).

    Returns:
        Same list with para_idx_start and para_idx_end set on each section dict.
    """
    if not heading_map:
        for section in sections:
            section.setdefault("para_idx_start", None)
            section.setdefault("para_idx_end", None)
        return sections

    heading_para_indices = [h[0] for h in heading_map]
    heading_texts = [h[1] for h in heading_map]

    _NUMBER_PREFIX_RE = re.compile(r"^\d+(\.\d+)*[\s.\-:]*")

    def _clean(title: str) -> str:
        return _NUMBER_PREFIX_RE.sub("", title).lower().strip()

    def _best_match_para_idx(section_title: str) -> int | None:
        clean = _clean(section_title)
        if not clean:
            return None
        cleaned_headings = [_clean(h) for h in heading_texts]
        matches = difflib.get_close_matches(clean, cleaned_headings, n=1, cutoff=0.4)
        if matches:
            pos = cleaned_headings.index(matches[0])
            return heading_para_indices[pos]
        return None

    result: list[dict] = []
    for i, section in enumerate(sections):
        sec = dict(section)
        start = _best_match_para_idx(sec.get("title", ""))
        if start is not None:
            sec["para_idx_start"] = start
            if i + 1 < len(sections):
                next_start = _best_match_para_idx(sections[i + 1].get("title", ""))
                sec["para_idx_end"] = (next_start - 1) if (next_start and next_start > start) else None
            else:
                sec["para_idx_end"] = total_paragraphs - 1
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
