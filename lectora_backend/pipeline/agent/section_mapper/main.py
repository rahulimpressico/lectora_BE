"""
Section Mapper — maps llm_to_outline sections to course_spec sections.

Supports two llm_to_outline formats and produces matching enriched_sections:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMAT 2 — flat (subtopics are strings, e.g. 533-style)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  llm_to_outline subtopics: ["2.1 Title", "2.2 Title", ...]

  Enriched subtopics (from course_spec):
  {
    "title":               str,
    "id":                  str,
    "has_knowledge_check": bool,
    "para_start":          int,
    "para_end":            int,
    "maps_to_objectives":  list,   # when present
    "images":              list,   # when present
    "image_count":         int,    # when present
    "interactive_elements": list,
  }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMAT 1 — breakdown (subtopics are objects with timing data, e.g. 529-style)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  llm_to_outline subtopics: [{"title": "2.1 ...", "word_count": "72", ...}, ...]

  Enriched subtopics (TO timing merged with course_spec para range):
  {
    "title":               str,   # from TO subtopic
    "content":             str,   # from TO subtopic
    "word_count":          str,   # from TO subtopic  ← used as generation budget
    "minutes":             str,   # from TO subtopic
    "credit_hour":         str,   # from TO subtopic
    "interactive_elements": list, # from TO subtopic (falls back to course_spec)
    "para_start":          int,   # from first assigned course_spec section
    "para_end":            int,   # from last  assigned course_spec section
    "id":                  str,   # from first course_spec section
    "has_knowledge_check": bool,  # True if any assigned course_spec section has KC
    "maps_to_objectives":  list,  # merged from all assigned course_spec sections
    "images":              list,  # merged from all assigned course_spec sections
    "image_count":         int,   # total images across assigned sections
  }

Result is saved to:
  - shared_state["agent_outputs"]["section_map"]["enriched_sections"]
  - enriched_sections.json  (sidecar next to shared_state.json, for debugging)
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lectora_backend.pipeline.shared_utils.kc_patterns import KC_RE as _KC_RE, is_kc_title as _is_kc_title

logger = logging.getLogger(__name__)


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


# Number prefix at start of a title:  "2.1", "3.2.1", "10.4"
_NUM_PREFIX_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\b")


def _title_number(title: str) -> str:
    """Extract the leading numeric prefix from a title (e.g. '2.1 Foo' -> '2.1').

    Returns empty string when no prefix is found.
    """
    if not isinstance(title, str):
        return ""
    m = _NUM_PREFIX_RE.match(title)
    return m.group(1) if m else ""


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
      1. **Title-prefix match**  — if the TO subtopic title starts with a
         numeric prefix (e.g. "2.1") AND any course_spec section's heading
         starts with the same prefix, group all consecutive course_spec
         sections that share that prefix (covers "2.1", "2.1.1", "2.1.2", …).
      2. **Proportional slice**  — fallback when no prefix overlap exists
         (legacy behaviour for documents without numbered subtopics).

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
        to_prefix = _title_number(to_sub.get("title", ""))
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


def _group_by_l1(sections: list[dict]) -> list[list[dict]]:
    """Group course_spec sections by their L1 chapter heading."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for sec in sections:
        if sec.get("level") == 1:
            if current:
                groups.append(current)
            current = [sec]
        else:
            current.append(sec)
    if current:
        groups.append(current)
    return groups


def _clean_ie(ie_list: list) -> list[str]:
    return [str(e) for e in ie_list if e and str(e).strip().lower() != "n/a"]


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

    if not spec_sections or not to_sections:
        return []

    is_breakdown = _is_breakdown_format(to_sections)
    logger.info("[SectionMapper] Detected format: %s", "breakdown (Format 1)" if is_breakdown else "flat (Format 2)")

    # -- Step 1: Group course_spec by L1 chapter --------------------------------
    groups   = _group_by_l1(spec_sections)
    n_groups = len(groups)
    n_to     = len(to_sections)

    # -- Step 2: Assign groups to TO sections -----------------------------------
    assignment: dict[int, list[list[dict]]] = {i: [] for i in range(n_to)}

    if n_groups >= 1 and n_to >= 1:
        if n_to == 1:
            # Single TO bucket (e.g. synthetic outline when no timed-outline file).
            assignment[0] = groups
        else:
            assignment[n_to - 1] = [groups[-1]]
            non_last = groups[:-1]
            n_avail  = len(non_last)
            n_head   = n_to - 1

            for i in range(n_head):
                start = round(i * n_avail / n_head) if n_head > 0 else 0
                end   = round((i + 1) * n_avail / n_head) if n_head > 0 else n_avail
                assignment[i] = non_last[start:end]

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

        # ── Format 2: standard course_spec subtopics ──────────────────────────
        else:
            has_children = any(s.get("level", 1) > 1 for s in all_secs)
            subtopics = [
                _build_subtopic_entry(sec)
                for sec in all_secs
                if not (has_children and sec.get("level", 1) == 1)
            ]
            # In Format 2, KC flag already lives on the spec section
            lesson_has_kc = any(s.get("has_knowledge_check") for s in all_secs)

        # Also check if any TO subtopic string says "Knowledge Check"
        to_str_subs = [s for s in (to_sec.get("subtopics") or []) if isinstance(s, str)]
        if any(_is_kc_title(s) for s in to_str_subs):
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


def run(shared_state_path: str) -> dict[str, Any]:
    """
    Execute section mapping: load shared_state → map → persist.

    Returns the result dict (same structure stored in shared_state).
    """
    now     = datetime.now(timezone.utc)
    ss_path = Path(shared_state_path).expanduser().resolve()
    ss_dir  = ss_path.parent

    with open(ss_path) as f:
        shared_state = json.load(f)

    run_id    = shared_state.get("run_id", "unknown")
    course_id = shared_state.get("request_spec", {}).get("course_metadata", {}).get("course_id")

    # -- Load course_spec from A1 output ----------------------------------------
    a1_output   = shared_state.get("agent_outputs", {}).get("A1", {})
    course_spec = a1_output.get("course_spec", {})
    if not course_spec:
        raise RuntimeError("[SectionMapper] A1 course_spec not found in shared_state")

    # -- Load llm_to_outline sidecar file ----------------------------------------
    outline_path = ss_dir / "llm_to_outline.json"
    if not outline_path.exists():
        raise RuntimeError(f"[SectionMapper] llm_to_outline not found: {outline_path}")

    with open(outline_path) as f:
        outline_data = json.load(f)
    outline = outline_data.get("llm_to_outline", {})
    to_totals: dict = outline.get("totals", {})

    # -- Run mapping -------------------------------------------------------------
    enriched_sections = map_sections(course_spec, outline)

    total_subtopics = sum(len(e.get("subtopics", [])) for e in enriched_sections)
    logger.info(
        "[SectionMapper] %s TO lessons → %s course_spec sections mapped.",
        len(enriched_sections),
        total_subtopics,
    )

    for lesson in enriched_sections:
        subs = lesson.get("subtopics", [])
        n_content = sum(1 for s in subs if not s.get("is_knowledge_check"))
        n_kc      = len(subs) - n_content
        logger.info("  [%s]  %s sections, %s KCs", lesson["title"][:45], n_content, n_kc)

    result = {
        "status":            "complete",
        "run_id":            run_id,
        "course_id":         course_id,
        "timestamp":         now.isoformat(),
        "to_totals":         to_totals,
        "enriched_sections": enriched_sections,
    }

    # -- Sidecar JSON (human-readable / debugging) --------------------------------
    out_path = ss_dir / "enriched_sections.json"
    with open(out_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("[SectionMapper] Saved: %s", out_path)

    # -- Update shared_state ------------------------------------------------------
    shared_state.setdefault("agent_outputs", {})["section_map"] = result
    shared_state["status"] = "section_map_complete"
    with open(ss_path, "w") as f:
        json.dump(shared_state, f, ensure_ascii=False, indent=2)
    logger.info("[SectionMapper] Shared state updated.")

    return result
