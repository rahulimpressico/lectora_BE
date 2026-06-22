"""
Section Mapper — orchestrator.

Loads shared state → runs map_sections → persists results.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..step_01_map_sections.utils.mapper import map_sections

logger = logging.getLogger(__name__)


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
        logger.warning(
            "[SectionMapper] llm_to_outline.json not found at %s — trying shared_state fallback",
            outline_path,
        )
        inline_to = shared_state.get("llm_to_outline_classification") or {}
        outline = (inline_to.get("llm_to_outline") or {}) or inline_to
        if not outline:
            raise RuntimeError(
                f"[SectionMapper] llm_to_outline not found at {outline_path} "
                "and no llm_to_outline_classification in shared_state"
            )
        logger.info("[SectionMapper] Using llm_to_outline_classification from shared_state")
    else:
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
