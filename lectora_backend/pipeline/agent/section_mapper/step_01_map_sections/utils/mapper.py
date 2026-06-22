"""Core section-mapping logic for Section Mapper."""
import logging

from lectora_backend.pipeline.shared_utils.kc_patterns import is_kc_title as _is_kc_title

from .matching import assign_groups_to_to_sections, best_fuzzy_spec_sections, subtopic_title
from .section_helpers import (
    _is_breakdown_format,
    _group_by_l1,
    _distribute_to_subtopics,
    _build_subtopic_entry,
    _clean_ie,
)

logger = logging.getLogger(__name__)


def map_sections(course_spec: dict, outline: dict) -> list[dict]:
    """
    Core mapping logic — supports both llm_to_outline formats.

    Format 2 (flat, subtopics are strings):
        Standard behaviour — course_spec sections become the enriched subtopics.

    Format 1 (breakdown, subtopics are objects with timing data):
        TO subtopic objects are merged with proportionally-assigned course_spec
        sections; the merged subtopics carry both TO timing fields AND
        course_spec para_start / para_end / KC flags / objectives.

    Returns one enriched entry per TO lesson.
    """
    spec_sections = course_spec.get("sections", [])
    to_sections   = outline.get("sections", [])

    if not spec_sections:
        raise RuntimeError("[SectionMapper] A1 course_spec has no sections — check A1 output")
    if not to_sections:
        logger.warning("[SectionMapper] No TO sections found — proceeding with empty mapping")
        return []

    is_breakdown = _is_breakdown_format(to_sections)
    logger.info("[SectionMapper] Detected format: %s", "breakdown (Format 1)" if is_breakdown else "flat (Format 2)")

    # -- Step 1: Group course_spec by L1 chapter --------------------------------
    groups = _group_by_l1(spec_sections)
    n_to = len(to_sections)

    # -- Step 2: Assign groups to TO sections (semantic match, not proportional) -
    assignment = assign_groups_to_to_sections(groups, to_sections)
    for to_idx, assigned_groups in assignment.items():
        if assigned_groups:
            labels = [g[0].get("heading", "?")[:40] for g in assigned_groups if g]
            logger.info(
                "[SectionMapper] TO lesson %d %r ← source group(s): %s",
                to_idx + 1,
                (to_sections[to_idx].get("title") or "")[:50],
                labels,
            )

    # -- Step 3: Build one enriched entry per TO lesson -------------------------
    enriched: list[dict] = []
    for to_idx, to_sec in enumerate(to_sections):
        all_secs: list[dict] = [
            sec
            for group in assignment.get(to_idx, [])
            for sec in group
        ]

        # ── Format 1: TO section has subtopic objects ─────────────────────────
        to_subtopic_objs: list[dict] = [
            s for s in (to_sec.get("subtopics") or []) if isinstance(s, dict)
        ]

        lesson_has_kc = False

        if is_breakdown and to_subtopic_objs:
            # Distribute the assigned course_spec sections across TO subtopic
            # objects and merge timing + para data into each subtopic.
            # KC-titled entries are stripped; has_kc captures their presence.
            subtopics, lesson_has_kc = _distribute_to_subtopics(to_subtopic_objs, all_secs)

        # ── Format 2: string subtopics or course_spec-backed subtopics ────────
        else:
            has_children = any(s.get("level", 1) > 1 for s in all_secs)
            to_str_subs = [
                s for s in (to_sec.get("subtopics") or []) if isinstance(s, str)
            ]
            real_str_subs = [s for s in to_str_subs if not _is_kc_title(s)]

            if real_str_subs and all_secs:
                # Map each TO subtopic string to the best source section by text.
                subtopics = []
                used_spec_ids: set[int] = set()
                for label in real_str_subs:
                    matched = best_fuzzy_spec_sections(
                        label,
                        all_secs,
                        exclude_ids=used_spec_ids,
                    )
                    if matched:
                        used_spec_ids.add(id(matched[0]))
                        subtopics.append(_build_subtopic_entry(matched[0]))
                for sec in all_secs:
                    if id(sec) in used_spec_ids:
                        continue
                    if has_children and sec.get("level", 1) == 1:
                        continue
                    subtopics.append(_build_subtopic_entry(sec))
            else:
                subtopics = [
                    _build_subtopic_entry(sec)
                    for sec in all_secs
                    if not (has_children and sec.get("level", 1) == 1)
                ]
            # In Format 2, KC flag already lives on the spec section
            lesson_has_kc = any(s.get("has_knowledge_check") for s in all_secs)

        # Also check if any TO subtopic string says "Knowledge Check"
        if any(_is_kc_title(subtopic_title(s)) for s in (to_sec.get("subtopics") or [])):
            lesson_has_kc = True

        lesson_ie = _clean_ie(to_sec.get("interactive_elements", []))
        if lesson_has_kc and "knowledge_check" not in lesson_ie:
            lesson_ie.append("knowledge_check")

        enriched.append({
            "title":                to_sec.get("title", f"Section {to_idx + 1}"),
            "content":              to_sec.get("content", ""),
            "word_count":           to_sec.get("word_count", ""),
            "minutes":              to_sec.get("minutes", ""),
            "credit_hour":          to_sec.get("credit_hour", ""),
            "interactive_elements": lesson_ie,
            "has_knowledge_check":  lesson_has_kc,
            "subtopics":            subtopics,
        })

    return enriched
