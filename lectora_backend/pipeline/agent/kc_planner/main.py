"""
KC Planner — determines which lessons/subtopics receive a Knowledge Check.

Runs after Section Mapper, before A2. Mutates has_knowledge_check on
enriched_sections[*] and enriched_sections[*].subtopics[*] in shared state.

Three scenarios, selected automatically:

  Scenario A — Raw doc has KCs, TO may or may not be present:
      A1 already set has_knowledge_check=True on sections that contain a
      "Knowledge Check" heading in the source .docx.  If a TO is available,
      we cross-reference: keep has_knowledge_check=True only for lessons
      where the TO confirms KC (via interactive_elements or KC-titled subtopic).
      If TO is absent, raw-doc flags are left as-is.

  Scenario B — Raw doc has NO KCs, TO is available:
      Derive KC placement purely from the TO.  For each TO lesson that declares
      KC (interactive_elements or KC-titled subtopic entry), mark the last
      substantive subtopic in that enriched lesson as has_knowledge_check=True.

  Scenario C — No KCs in raw doc, no TO:
      Algorithmically place KCs using kc_placement_rules from the active rule
      pack (cadence, forbidden_placements, min/max per lesson).
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack
from lectora_backend.pipeline.shared_utils.kc_patterns import KC_RE as _KC_RE, is_kc_title as _is_kc_title

logger = logging.getLogger(__name__)

_INTRO_RE = re.compile(r"\b(intro(?:duction)?|overview|preface|welcome)\b", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"\b(summary|conclusion|closing|review|recap|course\s+summary)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_forbidden_heading(title: str, forbidden_placements: list) -> bool:
    lower = (title or "").lower()
    for fp in forbidden_placements:
        keywords = str(fp).replace("_", " ").split()
        if all(kw in lower for kw in keywords if len(kw) > 3):
            return True
    return False


def _to_lesson_has_kc(to_lesson: dict) -> bool:
    """Return True if a TO lesson declares KC via interactive_elements or KC-titled subtopics."""
    ies = to_lesson.get("interactive_elements") or []
    if any(_KC_RE.search(str(ie)) for ie in ies):
        return True
    for sub in (to_lesson.get("subtopics") or []):
        if isinstance(sub, str) and _is_kc_title(sub):
            return True
        if isinstance(sub, dict) and _is_kc_title(sub.get("title", "")):
            return True
    return False


def _ensure_kc_in_ie(obj: dict) -> None:
    """Add 'knowledge_check' to obj['interactive_elements'] if not already present."""
    ie = obj.setdefault("interactive_elements", [])
    if "knowledge_check" not in ie:
        ie.append("knowledge_check")


def _remove_kc_from_ie(obj: dict) -> None:
    """Remove 'knowledge_check' from obj['interactive_elements'] if present."""
    ie = obj.get("interactive_elements")
    if ie and "knowledge_check" in ie:
        obj["interactive_elements"] = [x for x in ie if x != "knowledge_check"]


def _sync_lesson_kc_flag(lesson: dict) -> None:
    """Recalculate lesson-level has_knowledge_check from its subtopics."""
    has_kc = any(s.get("has_knowledge_check") for s in lesson.get("subtopics", []))
    lesson["has_knowledge_check"] = has_kc
    if has_kc:
        _ensure_kc_in_ie(lesson)
    else:
        _remove_kc_from_ie(lesson)


# ---------------------------------------------------------------------------
# Scenario A — raw doc KCs cross-referenced with TO
# ---------------------------------------------------------------------------

def _apply_scenario_a(
    enriched_sections: list,
    to_sections: list | None,
) -> tuple:
    """
    Keep raw-doc KC flags, but verify each lesson against the TO.

    For each lesson: if TO is present and does NOT declare KC, strip KC from
    all subtopics in that lesson.  If TO is absent, keep flags unchanged.
    """
    report: dict = {"scenario": "A", "decisions": []}

    for i, lesson in enumerate(enriched_sections):
        to_lesson = to_sections[i] if (to_sections and i < len(to_sections)) else None

        if to_lesson is None:
            # No TO — nothing to cross-reference; keep raw flags
            for sub in lesson.get("subtopics", []):
                if sub.get("has_knowledge_check"):
                    report["decisions"].append({
                        "lesson": lesson.get("title", ""),
                        "subtopic": sub.get("title", ""),
                        "decision": "kept_no_to",
                    })
            continue

        to_confirms = _to_lesson_has_kc(to_lesson)

        for sub in lesson.get("subtopics", []):
            original = bool(sub.get("has_knowledge_check"))
            if not original:
                continue

            if to_confirms:
                decision = "confirmed_by_to"
            else:
                sub["has_knowledge_check"] = False
                _remove_kc_from_ie(sub)
                decision = "removed_not_in_to"

            report["decisions"].append({
                "lesson": lesson.get("title", ""),
                "subtopic": sub.get("title", ""),
                "original": original,
                "final": sub.get("has_knowledge_check", original),
                "decision": decision,
            })

        _sync_lesson_kc_flag(lesson)

    return enriched_sections, report


# ---------------------------------------------------------------------------
# Scenario B — derive KC placement from TO (no raw-doc KCs)
# ---------------------------------------------------------------------------

def _apply_scenario_b(
    enriched_sections: list,
    to_sections: list,
) -> tuple:
    """
    For each TO lesson that declares KC, mark the last substantive subtopic
    in the corresponding enriched lesson as has_knowledge_check=True.
    """
    report: dict = {"scenario": "B", "decisions": []}

    for i, lesson in enumerate(enriched_sections):
        to_lesson = to_sections[i] if i < len(to_sections) else None
        if not to_lesson or not _to_lesson_has_kc(to_lesson):
            continue

        subtopics = lesson.get("subtopics", [])
        if not subtopics:
            continue

        # Find the best placement: last subtopic that is not a summary/conclusion
        found_target = False
        target_idx = len(subtopics) - 1
        for j in range(len(subtopics) - 1, -1, -1):
            heading = subtopics[j].get("title", subtopics[j].get("heading", ""))
            if not (_SUMMARY_RE.search(heading) or _INTRO_RE.search(heading)):
                target_idx = j
                found_target = True
                break

        if not found_target:
            logger.warning(
                "[KCPlanner] All subtopics in '%s' are summary/intro — skipping KC placement",
                lesson.get("title", ""),
            )
            continue

        subtopics[target_idx]["has_knowledge_check"] = True
        _ensure_kc_in_ie(subtopics[target_idx])
        _sync_lesson_kc_flag(lesson)

        report["decisions"].append({
            "lesson": lesson.get("title", ""),
            "subtopic": subtopics[target_idx].get("title", ""),
            "decision": "kc_from_to",
        })

    return enriched_sections, report


# ---------------------------------------------------------------------------
# Scenario C — rule-pack based algorithmic KC placement
# ---------------------------------------------------------------------------

def _pick_kc_indices(subtopics: list, kc_rules: dict) -> list:
    """
    Return a sorted list of subtopic indices that should receive a KC,
    based on cadence, forbidden_placements, and min/max per lesson from
    kc_placement_rules.
    """
    min_kc = int(kc_rules.get("min_kc_per_lesson", 2))
    max_kc = int(kc_rules.get("max_kc_per_lesson", 8))
    cadence = kc_rules.get("cadence") or {}
    screens_min = int(cadence.get("screens_min", 5))
    screens_max = int(cadence.get("screens_max", 10))
    cadence_step = max(1, (screens_min + screens_max) // 2)
    forbidden = kc_rules.get("forbidden_placements", [])

    # Build the candidate pool (skip forbidden, intro-first, summary-last)
    candidates: list = []
    for idx, sub in enumerate(subtopics):
        heading = sub.get("title", sub.get("heading", ""))
        if _is_forbidden_heading(heading, forbidden):
            continue
        if idx == 0 and _INTRO_RE.search(heading):
            continue
        if idx == len(subtopics) - 1 and _SUMMARY_RE.search(heading):
            continue
        candidates.append(idx)

    if not candidates:
        return []

    # Place KC at cadence_step intervals within the candidate pool
    selected: list = []
    next_at = cadence_step - 1  # 0-based index within candidates

    for pos, cand_idx in enumerate(candidates):
        if pos >= next_at:
            selected.append(cand_idx)
            next_at = pos + cadence_step
        if len(selected) >= max_kc:
            break

    # Guarantee min_kc by appending from end of candidates pool
    remaining = [c for c in reversed(candidates) if c not in selected]
    while len(selected) < min(min_kc, len(candidates)) and remaining:
        selected.append(remaining.pop(0))

    return sorted(set(selected))


def _apply_scenario_c(
    enriched_sections: list,
    kc_rules: dict,
) -> tuple:
    """Place KCs algorithmically using rule-pack kc_placement_rules."""
    report: dict = {"scenario": "C", "decisions": []}

    for lesson in enriched_sections:
        subtopics = lesson.get("subtopics", [])
        if not subtopics:
            continue

        indices = _pick_kc_indices(subtopics, kc_rules)
        for idx in indices:
            subtopics[idx]["has_knowledge_check"] = True
            _ensure_kc_in_ie(subtopics[idx])
            report["decisions"].append({
                "lesson": lesson.get("title", ""),
                "subtopic": subtopics[idx].get("title", ""),
                "decision": "kc_from_rule_pack",
            })

        if indices:
            _sync_lesson_kc_flag(lesson)

    return enriched_sections, report


# ---------------------------------------------------------------------------
# Coverage fill — ensures every lesson meets min_kc_per_lesson
# ---------------------------------------------------------------------------

def _fill_kc_coverage(enriched_sections: list, kc_rules: dict) -> list:
    """
    After Scenarios A or B, some lessons may have fewer KCs than
    min_kc_per_lesson (e.g. the TO did not declare KCs for every lesson).
    This pass finds those lessons and algorithmically adds KCs to close
    the gap — mirroring Scenario C logic on the undercovered lessons only.

    Called unconditionally after every scenario so Scenario C (which already
    guarantees coverage) is unaffected (no lesson will need filling).
    """
    min_kc = int(kc_rules.get("min_kc_per_lesson", 1))
    forbidden = kc_rules.get("forbidden_placements", [])

    for lesson in enriched_sections:
        subtopics = lesson.get("subtopics", [])
        if not subtopics:
            continue

        current_kc_count = sum(1 for s in subtopics if s.get("has_knowledge_check"))
        if current_kc_count >= min_kc:
            continue

        needed = min_kc - current_kc_count

        # Prefer placing KCs near the end of the lesson (best pedagogical position).
        # Skip already-KCd subtopics, intros at position 0, summaries at last position.
        candidates: list[int] = []
        for idx in range(len(subtopics) - 1, -1, -1):
            sub = subtopics[idx]
            if sub.get("has_knowledge_check"):
                continue
            heading = sub.get("title", sub.get("heading", ""))
            if _is_forbidden_heading(heading, forbidden):
                continue
            if idx == 0 and _INTRO_RE.search(heading):
                continue
            if idx == len(subtopics) - 1 and _SUMMARY_RE.search(heading):
                continue
            candidates.append(idx)
            if len(candidates) >= needed:
                break

        for idx in candidates:
            subtopics[idx]["has_knowledge_check"] = True
            _ensure_kc_in_ie(subtopics[idx])
            logger.info(
                "[KCPlanner] Coverage fill: added KC to '%s' / '%s'",
                lesson.get("title", ""),
                subtopics[idx].get("title", ""),
            )

        if candidates:
            _sync_lesson_kc_flag(lesson)

    return enriched_sections


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(shared_state_path: str) -> dict[str, Any]:
    """
    Execute KC planning: determine scenario, apply KC flags, persist results.

    Mutates enriched_sections in shared_state["agent_outputs"]["section_map"]
    and writes a kc_plan.json sidecar next to shared_state.json.

    Returns the result dict (also stored in shared_state["agent_outputs"]["kc_planner"]).
    """
    now = datetime.now(timezone.utc)
    ss_path = Path(shared_state_path).expanduser().resolve()
    ss_dir = ss_path.parent

    with open(ss_path) as f:
        shared_state = json.load(f)

    # -- 1. Load enriched_sections -------------------------------------------
    sm_output = shared_state.get("agent_outputs", {}).get("section_map", {})
    enriched_sections: list = sm_output.get("enriched_sections", [])
    if not enriched_sections:
        raise RuntimeError("[KCPlanner] No enriched_sections found — run Section Mapper first")

    # -- 2. Detect raw-doc KCs from A1 course_spec ---------------------------
    course_spec = (
        shared_state.get("agent_outputs", {})
        .get("A1", {})
        .get("course_spec", {})
    )
    sections = course_spec.get("sections")
    if sections is None:
        raise RuntimeError("[KCPlanner] course_spec missing 'sections' key — A1 output may be corrupted")
    if not sections:
        logger.warning("[KCPlanner] course_spec has empty sections list")
    raw_doc_has_kcs = any(
        s.get("has_knowledge_check", False) for s in (sections or [])
    )
    logger.info("[KCPlanner] Raw doc has KCs: %s", raw_doc_has_kcs)

    # -- 3. Load TO sections (optional) --------------------------------------
    to_sections: list | None = None
    outline_path = ss_dir / "llm_to_outline.json"
    if outline_path.exists():
        try:
            with open(outline_path) as f:
                outline_data = json.load(f)
            raw_to = (outline_data.get("llm_to_outline") or {}).get("sections") or []
            if raw_to:
                to_sections = raw_to
                logger.info("[KCPlanner] TO loaded: %s lessons", len(to_sections))
            else:
                logger.info("[KCPlanner] TO file present but has no sections — treated as absent")
        except Exception as exc:
            logger.warning("[KCPlanner] Cannot read llm_to_outline.json (%s) — TO treated as absent", exc)
    else:
        logger.info("[KCPlanner] llm_to_outline.json not found — trying shared_state fallback")
        inline = shared_state.get("llm_to_outline_classification") or {}
        inline_sections = (
            (inline.get("llm_to_outline") or {}).get("sections")
            or inline.get("sections")
            or []
        )
        if inline_sections:
            to_sections = inline_sections
            logger.info(
                "[KCPlanner] Using inline llm_to_outline_classification from shared_state (%s lessons)",
                len(to_sections),
            )
        else:
            logger.info("[KCPlanner] No TO sections in shared_state either — TO absent")

    has_to = bool(to_sections)

    # -- 4. Resolve rule pack for Scenario C ---------------------------------
    rule_family = (
        shared_state.get("request_spec", {})
        .get("rule_classification", {})
        .get("family")
    )
    rule_pack = (
        resolve_rule_pack(rule_family, shared_state.get("course_difficulty"))
        if rule_family
        else {}
    )
    kc_rules: dict = (rule_pack or {}).get("kc_placement_rules", {})

    # -- 5. Select and apply scenario ----------------------------------------
    if raw_doc_has_kcs:
        scenario = "A"
        logger.info(
            "[KCPlanner] Scenario A — raw-doc KCs present; cross-referencing with TO (%s)",
            "present" if has_to else "absent",
        )
        enriched_sections, report = _apply_scenario_a(
            enriched_sections,
            to_sections if has_to else None,
        )
    elif has_to:
        scenario = "B"
        logger.info("[KCPlanner] Scenario B — no raw-doc KCs; deriving KC placement from TO")
        enriched_sections, report = _apply_scenario_b(enriched_sections, to_sections)
    else:
        scenario = "C"
        logger.info("[KCPlanner] Scenario C — no KCs anywhere; placing KCs via rule-pack cadence")
        enriched_sections, report = _apply_scenario_c(enriched_sections, kc_rules)

    # Fill coverage gaps: Scenarios A and B may leave lessons with fewer KCs
    # than min_kc_per_lesson. Apply algorithmic placement to close the gap.
    if kc_rules:
        enriched_sections = _fill_kc_coverage(enriched_sections, kc_rules)

    kcs_total = sum(
        1
        for lesson in enriched_sections
        for sub in lesson.get("subtopics", [])
        if sub.get("has_knowledge_check")
    )
    logger.info(
        "[KCPlanner] Scenario %s done — %s KC(s) placed across all subtopics",
        scenario,
        kcs_total,
    )

    # -- 6. Persist ----------------------------------------------------------
    result = {
        "status": "complete",
        "scenario": scenario,
        "timestamp": now.isoformat(),
        "kc_count": kcs_total,
        "report": report,
    }

    # Update enriched_sections inside section_map (A2 reads from here)
    shared_state["agent_outputs"]["section_map"]["enriched_sections"] = enriched_sections
    shared_state["agent_outputs"]["kc_planner"] = result
    shared_state["status"] = "kc_plan_complete"

    with open(ss_path, "w") as f:
        json.dump(shared_state, f, ensure_ascii=False, indent=2)

    # Keep enriched_sections.json sidecar in sync
    enriched_path = ss_dir / "enriched_sections.json"
    if enriched_path.exists():
        try:
            with open(enriched_path) as f:
                es_data = json.load(f)
            es_data["enriched_sections"] = enriched_sections
            with open(enriched_path, "w") as f:
                json.dump(es_data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("[KCPlanner] Could not update enriched_sections.json sidecar: %s", exc)

    # Write KC plan sidecar for debugging
    kc_plan_path = ss_dir / "kc_plan.json"
    with open(kc_plan_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("[KCPlanner] kc_plan.json -> %s", kc_plan_path)
    logger.info("[KCPlanner] Shared state updated.")

    return result
