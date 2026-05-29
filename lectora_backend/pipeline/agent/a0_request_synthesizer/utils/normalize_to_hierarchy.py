"""
TO structure normalizer — ensures reserved sections are standalone and
course topics always appear as independent top-level sections.

Reserved sections: Overview, Introduction, Learning Objectives / Outcomes,
Course Objectives, Summary, Assessment.

These are metadata/structural sections rendered by A2 from extracted_inputs;
they must never act as parent containers for actual course topic sections.

The normalizer runs inside A0 immediately after the TO is generated or parsed
(before the result is written to llm_to_outline.json).
"""

import re

_RESERVED_RE = re.compile(
    r"^\s*(\d+(\.\d+)*\s+)?"          # optional leading "N.0 " / "N.0.M " prefix
    r"(overview|learning\s+objectives?|learning\s+outcomes?|course\s+objectives?|"
    r"summary|assessment|introduction)\s*$",
    re.IGNORECASE,
)

# Matches a leading numeric-dot prefix like "2.0 ", "2.0.1 ", "3.2.4 "
_NUM_PREFIX_RE = re.compile(r"^\s*\d+(?:\.\d+)+\s*")

# Imperative verbs that typically start LO statements (not topic headings)
_LO_VERB_RE = re.compile(
    r"^(understand|identify|describe|explain|recognize|list|apply|"
    r"demonstrate|analyze|analyse|evaluate|define|distinguish|compare|"
    r"discuss|summarize|recall|state|outline|assess|illustrate|examine|"
    r"review|know|learn|be able to)\b",
    re.IGNORECASE,
)


def _is_reserved(title: str) -> bool:
    return bool(_RESERVED_RE.match((title or "").strip()))


def _strip_number(title: str) -> str:
    """Remove any leading N.M(.P)+ prefix, e.g. '2.0.1 Foo' → 'Foo'."""
    return _NUM_PREFIX_RE.sub("", (title or "").strip()).strip()


def _looks_like_topic(sub) -> bool:
    """
    Return True when *sub* looks like a misplaced course topic rather than an
    LO statement or metadata item.

    Heuristics (in priority order):
    1. Numbered prefix (e.g. "2.0.1 Foo") — always a topic.
    2. Starts with an LO imperative verb — not a topic.
    3. Short title-case phrase (2-8 words) with no LO verb — likely a topic.
    4. Long sentence (≥ 9 words) — likely an LO statement.
    """
    if isinstance(sub, dict):
        title = (sub.get("title") or "").strip()
    elif isinstance(sub, str):
        title = sub.strip()
    else:
        return False
    if not title:
        return False

    # 1. Multi-level numeric prefix → definitely a course topic
    if re.match(r"^\d+\.\d+", title):
        return True

    # 2. LO imperative verb → not a topic
    if _LO_VERB_RE.match(title):
        return False

    # 3 / 4. Short phrase → topic; long sentence → LO
    word_count = len(title.split())
    return 2 <= word_count <= 8


def normalize_to_hierarchy(outline: dict) -> tuple[dict, bool]:
    """
    Scan the TO sections list and promote any course topics that are
    incorrectly nested as subtopics of a reserved section.

    Algorithm:
      For each reserved section:
        - Separate its subtopics into "topic-like" and "non-topic" (LO statements)
        - Clear the reserved section's subtopics (or keep only non-topic entries)
        - Append each promoted topic as a new independent top-level section

    After promotion, renumber all top-level sections sequentially so that
    reserved sections receive 1.0 / 2.0 and content lessons start at 3.0.

    Returns (normalized_outline, was_modified).
    """
    sections = list(outline.get("sections") or [])
    if not sections:
        return outline, False

    new_sections: list[dict] = []
    was_modified = False

    for sec in sections:
        title = (sec.get("title") or "").strip()
        subtopics = list(sec.get("subtopics") or [])

        if _is_reserved(title) or _is_reserved(_strip_number(title)):
            topic_subs = [s for s in subtopics if _looks_like_topic(s)]
            meta_subs  = [s for s in subtopics if not _looks_like_topic(s)]

            if topic_subs:
                was_modified = True

                # Keep reserved section with only metadata subtopics
                clean_sec = dict(sec)
                clean_sec["subtopics"] = meta_subs
                new_sections.append(clean_sec)

                # Promote each topic-like subtopic as a standalone top-level section
                for ts in topic_subs:
                    if isinstance(ts, dict):
                        promoted = dict(ts)
                        # Strip any multi-level number from the promoted title
                        t = (promoted.get("title") or "").strip()
                        promoted["title"] = _strip_number(t) or t
                        new_sections.append(promoted)
                    else:
                        new_sections.append({
                            "title":               _strip_number(str(ts)) or str(ts),
                            "content":             "",
                            "subtopics":           [],
                            "word_count":          "",
                            "minutes":             "",
                            "credit_hour":         "",
                            "interactive_elements": [],
                        })
            else:
                new_sections.append(sec)
        else:
            new_sections.append(sec)

    if not was_modified:
        return outline, False

    # Renumber all top-level sections so reserved slots keep 1.0/2.0 and
    # content lessons start at 3.0.  Minor numbering inside subtopics is
    # intentionally left intact — _renumber_sections in doc_formatter handles
    # the final sequential pass when writing the .docx.
    result = dict(outline)
    result["sections"] = _assign_top_level_numbers(new_sections)
    return result, True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assign_top_level_numbers(sections: list[dict]) -> list[dict]:
    """
    Give every top-level section a clean N.0 title.

    Reserved sections are pinned to 1.0 (Overview/Intro) or 2.0 (LO/Summary).
    All other sections are numbered 3.0, 4.0, … in document order.

    Numbers are stripped from subtopic titles too (they are renumbered later).
    """
    result: list[dict] = []
    content_major = 2   # first non-reserved section will be 3.0

    # First pass: assign fixed numbers to reserved sections
    seen_overview = False
    seen_lo       = False
    deferred_content: list[dict] = []

    for sec in sections:
        sec = dict(sec)
        title = (sec.get("title") or "").strip()
        clean = _strip_number(title) or title
        low   = clean.lower()

        if _is_reserved(title) or _is_reserved(clean):
            if ("overview" in low or "introduction" in low) and not seen_overview:
                sec["title"] = f"1.0 {clean}"
                seen_overview = True
                result.append(sec)
            elif not seen_lo:
                sec["title"] = f"2.0 {clean}"
                seen_lo = True
                result.append(sec)
            else:
                # More than one reserved section of the same kind — treat as content
                deferred_content.append(sec)
        else:
            deferred_content.append(sec)

    # Second pass: number the remaining content sections starting at 3.0
    for sec in deferred_content:
        content_major += 1
        title = (sec.get("title") or "").strip()
        clean = _strip_number(title) or title
        sec["title"] = f"{content_major}.0 {clean}"
        result.append(sec)

    return result
