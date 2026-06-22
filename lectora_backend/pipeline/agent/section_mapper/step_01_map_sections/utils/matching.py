"""
Title and paragraph-range matching for Section Mapper.

Learner-centric TO sections rarely share titles with A1 course_spec headings
(e.g. TO "Understanding Health Plan Obligations" vs source "ACA Employer Mandate").
These helpers score text similarity and acronym overlap so groups and subtopics
map to the correct source paragraph spans instead of proportional slicing.
"""
from __future__ import annotations

import difflib
import re
from functools import lru_cache
from typing import Any

_NUMBER_PREFIX_RE = re.compile(r"^\d+(\.\d+)*[\s.\-:]*")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}\b")
_WORD_RE = re.compile(r"[a-z]{3,}")

FUZZY_MATCH_CUTOFF = 0.40
GROUP_ASSIGN_CUTOFF = 0.35

EXACT_SUBSTRING_SCORE = 0.88
MAX_TOKEN_BOOST_SCORE = 0.92
PARA_OVERLAP_SCORE = 0.95


# ============================================================
# Title Matching
# ============================================================


@lru_cache(maxsize=2048)
def normalize_title(title: str) -> str:
    return _NUMBER_PREFIX_RE.sub("", (title or "").lower()).strip()


@lru_cache(maxsize=2048)
def extract_tokens(title: str) -> frozenset[str]:
    raw = title or ""

    tokens = {
        *[w.lower() for w in _ACRONYM_RE.findall(raw)],
        *[w.lower() for w in _WORD_RE.findall(raw.lower())],
    }

    return frozenset(t for t in tokens if len(t) >= 2)


def title_similarity(a: str, b: str) -> float:
    na = normalize_title(a)
    nb = normalize_title(b)

    if not na or not nb:
        return 0.0

    if na == nb:
        return 1.0

    if na in nb or nb in na:
        return EXACT_SUBSTRING_SCORE

    similarity = difflib.SequenceMatcher(None, na, nb).ratio()

    overlap_count = len(extract_tokens(a) & extract_tokens(b))
    if overlap_count:
        similarity = max(
            similarity,
            min(
                MAX_TOKEN_BOOST_SCORE,
                FUZZY_MATCH_CUTOFF + (0.18 * overlap_count),
            ),
        )

    return similarity


# ============================================================
# Paragraph Span Utilities
# ============================================================


def para_overlap(section: dict, start: int, end: int) -> bool:
    sec_start = section.get("para_start")
    sec_end = section.get("para_end")

    if sec_start is None or sec_end is None:
        return False

    return not (sec_end < start or sec_start > end)


def spec_sections_for_para_range(
    spec_sections: list[dict],
    para_start: int,
    para_end: int | None,
) -> list[dict]:
    end = para_end if para_end is not None else para_start

    return [
        section for section in spec_sections if para_overlap(section, para_start, end)
    ]


# ============================================================
# Spec Section Matching
# ============================================================


def best_fuzzy_spec_sections(
    label: str,
    spec_sections: list[dict],
    *,
    exclude_ids: set[int] | None = None,
    cutoff: float = FUZZY_MATCH_CUTOFF,
) -> list[dict]:
    if not label:
        return []

    exclude_ids = exclude_ids or set()

    best_section = None
    best_score = 0.0

    for section in spec_sections:
        if id(section) in exclude_ids:
            continue

        score = title_similarity(
            label,
            section.get("heading", ""),
        )

        if score > best_score:
            best_score = score
            best_section = section

    if best_section and best_score >= cutoff:
        return [best_section]

    return []


# ============================================================
# Group Matching
# ============================================================


def _subtopic_titles(to_section: dict) -> list[str]:
    titles = []

    for sub in to_section.get("subtopics") or []:
        if isinstance(sub, dict):
            title = sub.get("title", "")
        else:
            title = str(sub)

        if title.strip():
            titles.append(title)

    return titles


def group_match_score(
    to_section: dict,
    group: list[dict],
) -> float:
    if not group:
        return 0.0

    lesson_title = to_section.get("title", "")
    group_headings = [section.get("heading", "") for section in group]

    best_score = 0.0

    # Lesson ↔ Group
    for heading in group_headings:
        best_score = max(
            best_score,
            title_similarity(lesson_title, heading),
        )

    # Subtopic ↔ Group
    for subtopic in _subtopic_titles(to_section):
        for heading in group_headings:
            best_score = max(
                best_score,
                title_similarity(subtopic, heading),
            )

    # Paragraph overlap boost
    start = to_section.get("para_idx_start")

    if start is not None:
        end = to_section.get("para_idx_end", start)

        for section in group:
            if para_overlap(section, int(start), int(end)):
                best_score = max(
                    best_score,
                    PARA_OVERLAP_SCORE,
                )

    return best_score


# ============================================================
# Group Assignment
# ============================================================


def assign_groups_to_to_sections(
    groups: list[list[dict]],
    to_sections: list[dict],
) -> dict[int, list[list[dict]]]:

    assignments = {idx: [] for idx in range(len(to_sections))}

    if not groups or not to_sections:
        return assignments

    if len(to_sections) == 1:
        assignments[0] = list(groups)
        return assignments

    scored_pairs: list[tuple[float, int, int]] = []

    for group_idx, group in enumerate(groups):
        for lesson_idx, lesson in enumerate(to_sections):
            scored_pairs.append(
                (
                    group_match_score(lesson, group),
                    group_idx,
                    lesson_idx,
                )
            )

    scored_pairs.sort(reverse=True)

    assigned: set[int] = set()

    for score, group_idx, lesson_idx in scored_pairs:
        if group_idx in assigned:
            continue

        if score < GROUP_ASSIGN_CUTOFF:
            continue

        assignments[lesson_idx].append(groups[group_idx])
        assigned.add(group_idx)

    # fallback balancing
    for group_idx, group in enumerate(groups):
        if group_idx in assigned:
            continue

        target = min(
            assignments,
            key=lambda idx: len(assignments[idx]),
        )

        assignments[target].append(group)

    return assignments


def subtopic_title(sub: Any) -> str:
    if isinstance(sub, dict):
        return str(sub.get("title") or "")
    return str(sub or "")
