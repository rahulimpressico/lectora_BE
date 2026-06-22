"""Step 05 — LLM enrichment: subtopics + LO mapping."""
import json
import logging
import re

from ...config.llm import chat
from ...shared.helpers.section_helpers import _is_reserved_section
from ...shared.models.state import A1State
from ..constants.prompts import ENRICH_SYSTEM

logger = logging.getLogger(__name__)


def enrich_with_llm(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Enriching sections with AzureOpenAI (subtopics + LO mapping)...")
    los = state["a0_data"].get("extracted_inputs", {}).get("learning_objectives", [])

    section_input = {}
    for s in state["raw_sections"]:
        if _is_reserved_section(s["heading"]):
            continue
        preview = " ".join(s["paragraphs"][:2])[:250] if s["paragraphs"] else ""
        section_input[s["heading"]] = {"preview": preview}

    payload: dict = {
        "learning_objectives": {str(i): lo for i, lo in enumerate(los)},
        "sections": section_input,
    }
    fb = state.get("feedback")
    if fb:
        vf = fb.get("validator_feedback")
        if vf:
            payload["validator_feedback"] = vf
        att = fb.get("attempt")
        if att is not None:
            payload["retry_attempt"] = att

    try:
        raw = chat(ENRICH_SYSTEM, json.dumps(payload, ensure_ascii=False))
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        enrichment = json.loads(raw)
        logger.info("[A1] LLM enriched %s sections.", len(enrichment))
        return {**state, "enrichment": enrichment, "error": None}
    except Exception as e:
        logger.warning("[A1] LLM enrichment failed: %s — continuing without enrichment.", e)
        return {**state, "enrichment": {}, "error": f"enrich_with_llm failed: {e}"}
