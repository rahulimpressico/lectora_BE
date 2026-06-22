"""Step 06 — Assemble final course_spec from parsed + enriched data."""
import logging

from ...shared.helpers.section_helpers import _is_reserved_section, _normalize_section_level
from ...shared.models.state import A1State
from ...shared.utils.text_utils import words_to_minutes, wpm_from_rule_pack
from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack
from lectora_backend.pipeline.shared_utils.interactive_elements import resolve_section_assets

logger = logging.getLogger(__name__)


def build_course_spec(state: A1State) -> A1State:
    """Assemble course_spec from raw_sections (parse_document) + LLM enrichment."""
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Assembling course_spec from parsed + enriched data...")
    enrichment = state.get("enrichment", {})
    sections_out = []

    rule_family = (
        state["a0_data"].get("request_spec", {})
        .get("rule_classification", {})
        .get("family")
    )
    rule_pack = resolve_rule_pack(rule_family) if rule_family else None
    wpm = wpm_from_rule_pack(rule_pack or {}, default=180)
    logger.info("[A1] Pacing: %s words/min (derived from rule pack)", wpm)

    for s in state["raw_sections"]:
        heading = s["heading"]
        enrich = enrichment.get(heading, {})
        para_start = s["para_start"]
        para_end = max(s["para_end"], para_start)
        level = _normalize_section_level(s["level"])

        mapped_images = [
            im
            for im in state.get("image_map", {}).get(s["id"], [])
            if para_start <= im.get("para_idx", -1) <= para_end
        ]
        raw_ies, section_images = resolve_section_assets(
            s.get("interactive_elements", []),
            mapped_images,
            has_knowledge_check=bool(s.get("has_knowledge_check")),
        )
        has_kc_final = "knowledge_check" in raw_ies
        wc = s.get("word_count", 0) or 0

        sections_out.append({
            "id": s["id"],
            "heading": heading,
            "level": level,
            "is_reserved": _is_reserved_section(heading),
            "is_knowledge_check": s["is_knowledge_check"],
            "has_knowledge_check": has_kc_final,
            "para_start": para_start,
            "para_end": para_end,
            "word_count": wc,
            "estimated_duration_minutes": round(words_to_minutes(wc, wpm=wpm), 2) if wc else 0.0,
            "interactive_elements": list(raw_ies),
            "maps_to_objectives": enrich.get("maps_to_objectives", []),
            "images": section_images,
            "image_count": len(section_images),
        })

    a0_inputs = state["a0_data"].get("extracted_inputs", {})
    course_spec = {
        "run_id": state["run_id"],
        "course_id": a0_inputs.get("course_id"),
        "course_title": a0_inputs.get("title"),
        "extracted_inputs": {
            "title": a0_inputs.get("title"),
            "course_id": a0_inputs.get("course_id"),
            "learning_objectives": a0_inputs.get("learning_objectives", []),
        },
        "sections": sections_out,
    }

    logger.info("[A1] course_spec built: %s sections.", len(sections_out))
    return {**state, "course_spec": course_spec}
