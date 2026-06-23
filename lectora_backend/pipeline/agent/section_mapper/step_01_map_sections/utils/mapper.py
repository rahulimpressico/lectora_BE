"""
Section Mapper — core mapping logic.

Architecture
────────────
  Retrieval Layer  → vector_retriever.py   (Azure AI Search: embed → search → rank)
  Mapping Layer    → this file             (TO structure + metadata propagation)
  Generation Layer → A2 content generator  (consumes matched_chunks per subtopic)

Flow (per TO lesson)
────────────────────
  1. Build subtopic list from the TO outline (Format 1: timed objects; Format 2: strings).
  2. Propagate KC flags and objective indices from A1 course_spec via proportional
     index distribution — no fuzzy text matching required.
  3. Retrieve semantically relevant source chunks via Azure AI Search hybrid search.
  4. Distribute chunks to subtopics via per-subtopic Azure AI Search retrieval
     with semantic ranking — no keyword-overlap or cosine-similarity heuristics.

Output contract (enriched_sections)
────────────────────────────────────
  One dict per TO lesson:
    title, content, word_count, minutes, credit_hour,
    interactive_elements, has_knowledge_check,
    subtopics: [
      title, word_count, minutes, credit_hour,
      interactive_elements, para_start, para_end,
      has_knowledge_check, maps_to_objectives,
      images, image_count,
      matched_chunks: [{raw_text, similarity_score, source_metadata}]
    ]
"""
from __future__ import annotations

import logging

from lectora_backend.pipeline.shared_utils.kc_patterns import is_kc_title as _is_kc_title

from .section_helpers import _clean_ie, _is_breakdown_format
from .vector_retriever import get_retriever

logger = logging.getLogger(__name__)

# Diagnostic threshold: log a warning when average vector chunks per subtopic
# falls below this value (search returned thin results).
_LOW_COVERAGE_THRESHOLD = 1


# ── Utilities ──────────────────────────────────────────────────────────────────

def _subtopic_title(sub) -> str:
    """Return the title string from a subtopic (string or dict)."""
    if isinstance(sub, dict):
        return str(sub.get("title") or "")
    return str(sub or "")


# ── Metadata propagation from course_spec ─────────────────────────────────────

def _build_spec_meta(spec_sections: list[dict], n_lessons: int) -> dict[int, dict]:
    """
    Distribute A1 course_spec metadata to TO lesson slots by proportional index.

    Preserves KC flags, objective-index lists, and image references from A1
    without any text-similarity matching.  Each lesson slot gets the metadata
    from its proportional slice of spec sections.

    Returns:
        {lesson_idx: {"has_kc": bool, "objectives": list[int], "images": list}}
    """
    if not spec_sections or n_lessons == 0:
        return {}

    n_specs = len(spec_sections)
    result: dict[int, dict] = {}

    for lesson_idx in range(n_lessons):
        start = round(lesson_idx * n_specs / n_lessons)
        end = round((lesson_idx + 1) * n_specs / n_lessons)
        slice_ = spec_sections[start:end]

        has_kc = any(s.get("has_knowledge_check") for s in slice_)

        objectives: list[int] = []
        for s in slice_:
            objectives.extend(s.get("maps_to_objectives") or [])
        objectives = list(dict.fromkeys(objectives))  # unique, order-preserving

        images: list = []
        for s in slice_:
            images.extend(s.get("images") or [])

        result[lesson_idx] = {
            "has_kc":     has_kc,
            "objectives": objectives,
            "images":     images,
        }

    return result


# ── Subtopic builders ──────────────────────────────────────────────────────────

def _build_breakdown_subtopics(
    to_sec: dict,
    lesson_objectives: list[int],
    lesson_images: list,
) -> list[dict]:
    """
    Build subtopic entries for Format 1 (TO subtopics are objects with timing data).

    KC-titled entries are excluded from the list; the caller detects KC presence
    separately from the TO interactive_elements and title patterns.
    """
    subtopics: list[dict] = []
    to_subs = [s for s in (to_sec.get("subtopics") or []) if isinstance(s, dict)]

    for i, sub in enumerate(to_subs):
        title = sub.get("title", "")
        if _is_kc_title(title):
            continue

        # Assign images to the first content subtopic of the lesson only, so
        # they appear near the start of the generated content rather than being
        # duplicated across every subtopic in A2's docx renderer.
        images = lesson_images if (i == 0 and lesson_images) else []

        subtopics.append({
            "title":                title,
            "content":              sub.get("content", ""),
            "word_count":           sub.get("word_count", ""),
            "minutes":              sub.get("minutes", ""),
            "credit_hour":          sub.get("credit_hour", ""),
            "interactive_elements": _clean_ie(sub.get("interactive_elements") or []),
            "para_start":           0,
            "para_end":             0,
            "has_knowledge_check":  False,
            "maps_to_objectives":   lesson_objectives,
            "images":               images,
            "image_count":          len(images),
        })

    return subtopics


def _build_flat_subtopics(
    to_sec: dict,
    lesson_objectives: list[int],
    lesson_images: list,
) -> list[dict]:
    """
    Build subtopic entries for Format 2 (TO subtopics are strings, or absent).

    KC-titled strings are excluded; the caller detects KC presence separately.
    """
    subtopics: list[dict] = []
    to_subs = to_sec.get("subtopics") or []

    for i, sub in enumerate(to_subs):
        title = _subtopic_title(sub)
        if not title or _is_kc_title(title):
            continue

        images = lesson_images if (i == 0 and lesson_images) else []

        subtopics.append({
            "title":                title,
            "interactive_elements": [],
            "para_start":           0,
            "para_end":             0,
            "has_knowledge_check":  False,
            "maps_to_objectives":   lesson_objectives,
            "images":               images,
            "image_count":          len(images),
        })

    return subtopics


# ── KC detection ───────────────────────────────────────────────────────────────

def _detect_lesson_kc(to_sec: dict, from_spec: bool) -> bool:
    """
    Return True when this TO lesson should have a Knowledge Check.

    Sources (checked in order):
      1. KC flag propagated from A1 course_spec (proportional distribution).
      2. TO section has 'knowledge_check' in its interactive_elements list.
      3. Any TO subtopic title matches the KC title pattern (kc_patterns.py).
    """
    if from_spec:
        return True
    if "knowledge_check" in (to_sec.get("interactive_elements") or []):
        return True
    return any(
        _is_kc_title(_subtopic_title(s))
        for s in (to_sec.get("subtopics") or [])
    )


# ── Vector enrichment ──────────────────────────────────────────────────────────

def _enrich_with_vector_chunks(
    retriever,
    to_idx: int,
    lesson_title: str,
    subtopics: list[dict],
    lesson_objectives: list[int],
    spec_learning_objectives: list[str] | None,
) -> None:
    """
    Fetch source chunks from Azure AI Search and distribute them to subtopics.

    Mutates subtopics in-place by adding 'matched_chunks' to each entry.
    Logs query, retrieved chunks, similarity scores, and mapping decisions.
    Silently skips on network errors so the pipeline never blocks on retrieval.

    Args:
        retriever:                  VectorRetriever instance.
        to_idx:                     Lesson index (for logging).
        lesson_title:               TO lesson heading (used in query).
        subtopics:                  Mutable list of subtopic dicts.
        lesson_objectives:          Objective indices for this lesson.
        spec_learning_objectives:   Natural-language objective strings (from A0
                                    extracted_inputs); used to build a richer query.
                                    May be None when not available.
    """
    sub_titles = [s["title"] for s in subtopics if s.get("title")]

    # Build natural-language objectives from objective indices when available.
    nl_objectives: list[str] | None = None
    if lesson_objectives and spec_learning_objectives:
        nl_objectives = [
            spec_learning_objectives[i]
            for i in lesson_objectives
            if 0 <= i < len(spec_learning_objectives)
        ][:4]

    try:
        lesson_chunks = retriever.retrieve_for_lesson(
            lesson_title=lesson_title,
            subtopic_titles=sub_titles,
            objectives=nl_objectives,
        )
    except Exception as exc:
        logger.warning(
            "[SectionMapper] Vector retrieval failed for lesson %d %r: %s",
            to_idx + 1, lesson_title[:50], exc,
        )
        return

    if not lesson_chunks:
        logger.debug(
            "[SectionMapper] Lesson %d %r — 0 chunks returned by vector search",
            to_idx + 1, lesson_title[:50],
        )
        return

    retriever.distribute_to_subtopics(lesson_chunks, subtopics, lesson_title=lesson_title)

    chunk_counts = [len(s.get("matched_chunks", [])) for s in subtopics]
    total = sum(chunk_counts)
    logger.info(
        "[SectionMapper] Lesson %d %r — %d chunks distributed → %s per subtopic",
        to_idx + 1, lesson_title[:50], total, chunk_counts,
    )

    avg = total / len(subtopics) if subtopics else 0
    if avg < _LOW_COVERAGE_THRESHOLD:
        logger.warning(
            "[SectionMapper] Lesson %d — low vector coverage (avg %.1f chunks/subtopic). "
            "Ensure the source document is indexed before running the pipeline.",
            to_idx + 1, avg,
        )


# ── Public entry point ─────────────────────────────────────────────────────────

def map_sections(course_spec: dict, outline: dict) -> list[dict]:
    """
    Map TO lesson structure to source content via Azure AI Search vector retrieval.

    Supports both TO outline formats:
      Format 1 (breakdown) — subtopics are objects carrying timing data.
      Format 2 (flat)      — subtopics are plain strings (or absent).

    Returns one enriched entry per TO lesson.  Each subtopic carries:
      - Structural metadata from the TO outline (title, timing).
      - KC flags and objective indices propagated from A1 course_spec.
      - matched_chunks: vector-retrieved source content ready for A2.
    """
    spec_sections = course_spec.get("sections", [])
    to_sections = outline.get("sections", [])

    if not to_sections:
        logger.warning("[SectionMapper] No TO sections found — returning empty mapping")
        return []

    is_breakdown = _is_breakdown_format(to_sections)
    logger.info(
        "[SectionMapper] TO format: %s | %d lessons | %d spec sections",
        "breakdown (Format 1)" if is_breakdown else "flat (Format 2)",
        len(to_sections),
        len(spec_sections),
    )

    # Propagate KC flags, objective indices, and images from A1 course_spec.
    # Uses proportional index distribution — no text matching.
    spec_meta = _build_spec_meta(spec_sections, len(to_sections))

    # Natural-language learning objectives (from A0 extracted_inputs, if present
    # in shared_state).  Passed through outline as a side channel when available.
    spec_lo_strings: list[str] | None = (
        outline.get("_learning_objectives_text") or None
    )

    # Initialise vector retriever — returns None when Azure AI Search is not
    # configured, allowing the pipeline to run without blocking.
    retriever = get_retriever()
    if retriever:
        logger.info("[SectionMapper] Azure AI Search retriever active")
    else:
        logger.warning(
            "[SectionMapper] Azure AI Search not configured — "
            "subtopics will have no matched_chunks; A2 will rely on BM25 fallback"
        )

    enriched: list[dict] = []

    for to_idx, to_sec in enumerate(to_sections):
        meta = spec_meta.get(to_idx, {})
        lesson_objectives: list[int] = meta.get("objectives", [])
        lesson_images: list = meta.get("images", [])
        has_kc_from_spec: bool = meta.get("has_kc", False)

        # Build subtopics from TO structure
        if is_breakdown:
            subtopics = _build_breakdown_subtopics(to_sec, lesson_objectives, lesson_images)
        else:
            subtopics = _build_flat_subtopics(to_sec, lesson_objectives, lesson_images)

        lesson_title = to_sec.get("title", f"Section {to_idx + 1}")
        lesson_has_kc = _detect_lesson_kc(to_sec, has_kc_from_spec)

        logger.info(
            "[SectionMapper] Lesson %d: %r  subtopics=%d  kc=%s",
            to_idx + 1, lesson_title[:60], len(subtopics), lesson_has_kc,
        )

        # Vector retrieval — augments each subtopic with matched_chunks
        if retriever and subtopics:
            _enrich_with_vector_chunks(
                retriever=retriever,
                to_idx=to_idx,
                lesson_title=lesson_title,
                subtopics=subtopics,
                lesson_objectives=lesson_objectives,
                spec_learning_objectives=spec_lo_strings,
            )

        lesson_ie = _clean_ie(to_sec.get("interactive_elements") or [])
        if lesson_has_kc and "knowledge_check" not in lesson_ie:
            lesson_ie.append("knowledge_check")

        enriched.append({
            "title":                lesson_title,
            "content":              to_sec.get("content", ""),
            "word_count":           to_sec.get("word_count", ""),
            "minutes":              to_sec.get("minutes", ""),
            "credit_hour":          to_sec.get("credit_hour", ""),
            "interactive_elements": lesson_ie,
            "has_knowledge_check":  lesson_has_kc,
            "subtopics":            subtopics,
        })

    return enriched
