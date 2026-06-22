"""Step 03 — Learning-objective validation."""
import logging
from ...shared.models.state import A1State

logger = logging.getLogger(__name__)


def validate_los(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Validating learning objectives...")
    los = state["a0_data"].get("extracted_inputs", {}).get("learning_objectives", [])

    if not los:
        sections = state.get("raw_sections", [])
        if sections:
            logger.warning("[A1] No learning objectives found — continuing without LOs (sections present).")
            return {**state, "status": "complete", "error": "Missing LOs — proceeding without them"}
        else:
            logger.error("[A1] CRITICAL — no learning objectives and no sections. Stopping pipeline.")
            return {**state, "status": "stopped", "error": "No sections and no LOs — cannot proceed"}

    logger.info("[A1] %s learning objectives confirmed.", len(los))
    return state
