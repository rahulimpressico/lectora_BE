"""
Section-mapping helper functions for building and distributing subtopics.

Handles both Format 1 (breakdown — TO subtopics are objects with timing data)
and Format 2 (flat — TO subtopics are strings or course_spec-backed entries).
"""
from __future__ import annotations

from lectora_backend.pipeline.shared_utils.kc_patterns import is_kc_title as _is_kc_title

from ..constants.patterns import _NUM_PREFIX_RE, _is_reserved_heading
from .matching import best_fuzzy_spec_sections, spec_sections_for_para_range


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def _is_breakdown_format(to_sections: list[dict]) -> bool:
    """Return True if at least one TO section has subtopics as objects (Format 1)."""
    return any(
        isinstance(sub, dict)
        for sec in to_sections
        for sub in sec.get("subtopics", [])
    )


def _title_number(title: str) -> str:
    """Extract the leading numeric prefix from a title (e.g. '2.1 Foo' -> '2.1').

    Returns empty string when no prefix is found.
    """
    if not isinstance(title, str):
        return ""
    m = _NUM_PREFIX_RE.match(title)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Shared utility
# ---------------------------------------------------------------------------

def _clean_ie(ie_list: list) -> list[str]:
    return [str(e) for e in ie_list if e and str(e).strip().lower() != "n/a"]


# ---------------------------------------------------------------------------
# Format 2 helpers
# ---------------------------------------------------------------------------

def _build_subtopic_entry(sec: dict) -> dict:
    """Convert a course_spec section dict into the slim subtopic shape.
    Only includes fields that have meaningful data — empty lists are omitted."""
    entry: dict = {
        "title":               sec.get("heading", ""),
        "id":                  sec.get("id", ""),
        "has_knowledge_check": sec.get("has_knowledge_check", False),
        "para_start":          sec.get("para_start", 0),
        "para_end":            sec.get("para_end", 0),
    }
    if sec.get("subtopics"):
        entry["subtopics"] = sec["subtopics"]
    if sec.get("maps_to_objectives"):
        entry["maps_to_objectives"] = sec["maps_to_objectives"]
    if sec.get("images"):
        entry["images"] = sec["images"]
    if sec.get("image_count"):
        entry["image_count"] = sec["image_count"]
    # Always carry the section-level IE list — even if empty — so content_writer
    # never falls back to the lesson (TO outline) IE for this subtopic.
    entry["interactive_elements"] = sec.get("interactive_elements", [])
    return entry


def _group_by_l1(sections: list[dict]) -> list[list[dict]]:
    """Group course_spec sections by their L1 chapter heading.

    Reserved sections (Overview, Learning Objectives, Summary, Assessment, etc.)
    are excluded entirely — they are rendered by A2 from metadata and must not
    act as containers for actual course topics.  Any sub-level sections that
    immediately follow a reserved L1 heading are also skipped (they are LO item
    lines or overview paragraphs, not course content).
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    in_reserved_block = False

    for sec in sections:
        heading = sec.get("heading", "")
        is_res = sec.get("is_reserved") or _is_reserved_heading(heading)

        if sec.get("level") == 1:
            in_reserved_block = is_res
            if is_res:
                # Flush any open group but do NOT start a new group for this heading.
                if current:
                    groups.append(current)
                    current = []
                continue

            # Normal L1 content heading — start a new group.
            if current:
                groups.append(current)
            current = [sec]

        else:
            if in_reserved_block:
                # Sub-sections under a reserved L1 (e.g., LO bullet items parsed
                # as H2) are metadata, not content — skip them.
                continue
            current.append(sec)

    if current:
        groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# Format 1 helpers
# ---------------------------------------------------------------------------

def _build_breakdown_subtopic(to_sub: dict, spec_secs: list[dict]) -> dict:
    """
    Merge a TO subtopic object (timing data) with its assigned course_spec
    sections (para_start / para_end / KC flags / objectives / images).

    The TO fields are kept as-is; course_spec fields are derived by:
      - para_start  = first spec section's para_start
      - para_end    = last  spec section's para_end
      - has_knowledge_check = True if ANY spec section has KC
      - maps_to_objectives / images  = union across all spec sections
    """
    entry: dict = {
        "title":                to_sub.get("title", ""),
        "content":              to_sub.get("content", ""),
        "word_count":           to_sub.get("word_count", ""),
        "minutes":              to_sub.get("minutes", ""),
        "credit_hour":          to_sub.get("credit_hour", ""),
        "interactive_elements": list(to_sub.get("interactive_elements") or []),
        # defaults — overwritten below when spec_secs is non-empty
        "para_start":          0,
        "para_end":            0,
        "id":                  "",
        "has_knowledge_check": False,
    }

    if spec_secs:
        entry["para_start"]          = spec_secs[0].get("para_start", 0)
        entry["para_end"]            = spec_secs[-1].get("para_end", 0)
        entry["id"]                  = spec_secs[0].get("id", "")
        entry["has_knowledge_check"] = any(s.get("has_knowledge_check") for s in spec_secs)

        objectives: list = []
        for s in spec_secs:
            objectives.extend(s.get("maps_to_objectives") or [])
        if objectives:
            entry["maps_to_objectives"] = list(dict.fromkeys(objectives))

        images: list = []
        for s in spec_secs:
            images.extend(s.get("images") or [])
        if images:
            entry["images"]      = images
            entry["image_count"] = len(images)

        # If the TO subtopic has no IE, fall back to course_spec IE
        if not entry["interactive_elements"]:
            spec_ie: list = []
            for s in spec_secs:
                spec_ie.extend(s.get("interactive_elements") or [])
            entry["interactive_elements"] = _clean_ie(spec_ie)

    return entry


def _distribute_to_subtopics(
    to_subtopic_objs: list[dict],
    spec_secs: list[dict],
) -> tuple[list[dict], bool]:
    """
    Merge each TO subtopic object with its matching course_spec sections.

    Strategy (best → worst):
      1. **Paragraph range** — TO subtopic has para_idx_start/end overlapping
         a course_spec section span.
      2. **Fuzzy title match** — learner-centric subtopic text vs spec heading
         (acronym overlap e.g. HIPAA, ACA).
      3. **Title-prefix match**  — numeric prefix (e.g. "2.1") on both sides.
      4. **Proportional slice**  — fallback when nothing else matches.

    Knowledge Check entries are stripped from the distribution — they should
    never become content sections.  Returns (enriched_subtopics, has_kc).
    """
    real_subs = [s for s in to_subtopic_objs if not _is_kc_title(s.get("title", ""))]
    has_kc = len(real_subs) < len(to_subtopic_objs)

    if not real_subs:
        return [], has_kc

    # Build a map  prefix -> [spec sections]  preserving document order.
    # Sections without a numeric prefix go through the leftover_specs path
    # below (as fallback for unmatched TO subs).
    spec_prefix_groups: dict[str, list[dict]] = {}
    for sec in spec_secs:
        prefix = _title_number(sec.get("heading", ""))
        if prefix:
            spec_prefix_groups.setdefault(prefix, []).append(sec)

    def _match_by_prefix(to_prefix: str) -> list[dict]:
        """Return all spec sections whose prefix == to_prefix or starts with to_prefix + '.'.

        E.g. to_prefix '2.1' matches '2.1', '2.1.1', '2.1.2' (deeper children).
        """
        if not to_prefix:
            return []
        out: list[dict] = []
        for spec_pref, group in spec_prefix_groups.items():
            if spec_pref == to_prefix or spec_pref.startswith(to_prefix + "."):
                out.extend(group)
        return out

    # First pass: prefix matching
    matched: list[tuple[dict, list[dict]]] = []
    unmatched_to: list[int] = []
    used_spec_ids: set[int] = set()

    for idx, to_sub in enumerate(real_subs):
        title = to_sub.get("title", "")
        assigned: list[dict] = []

        # 1) Explicit para range on the TO subtopic object
        sub_start = to_sub.get("para_idx_start")
        if sub_start is not None:
            sub_end = to_sub.get("para_idx_end", sub_start)
            assigned = spec_sections_for_para_range(spec_secs, int(sub_start), int(sub_end))

        # 2) Fuzzy title ↔ heading (learner-centric TO subtopics)
        if not assigned:
            assigned = best_fuzzy_spec_sections(
                title,
                spec_secs,
                exclude_ids=used_spec_ids,
            )

        # 3) Numeric prefix match (legacy numbered outlines)
        if not assigned:
            to_prefix = _title_number(title)
            assigned = _match_by_prefix(to_prefix)

        if assigned:
            matched.append((to_sub, assigned))
            used_spec_ids.update(id(s) for s in assigned)
        else:
            matched.append((to_sub, []))
            unmatched_to.append(idx)

    # Anything in spec_secs not yet matched (e.g. unprefixed sub-content)
    leftover_specs = [s for s in spec_secs if id(s) not in used_spec_ids]

    # If we have unmatched TO subs AND leftover spec secs → distribute
    # leftovers proportionally across the unmatched TO subs.
    if unmatched_to and leftover_specs:
        n_un = len(unmatched_to)
        n_left = len(leftover_specs)
        for k, to_idx in enumerate(unmatched_to):
            start = round(k * n_left / n_un)
            end = round((k + 1) * n_left / n_un)
            slice_ = leftover_specs[start:end]
            if slice_:
                to_sub, _ = matched[to_idx]
                matched[to_idx] = (to_sub, slice_)

    # If NO prefix match worked at all (legacy doc with no numbering)
    # fall back to the original proportional algorithm.
    nothing_matched = all(len(secs) == 0 for _, secs in matched)
    if nothing_matched:
        n_to = len(real_subs)
        n_specs = len(spec_secs)
        result: list[dict] = []
        for i, to_sub in enumerate(real_subs):
            start = round(i * n_specs / n_to)
            end = round((i + 1) * n_specs / n_to)
            result.append(_build_breakdown_subtopic(to_sub, spec_secs[start:end]))
        return result, has_kc

    # Build final enriched list preserving the TO order
    result = [_build_breakdown_subtopic(to_sub, secs) for to_sub, secs in matched]
    return result, has_kc
