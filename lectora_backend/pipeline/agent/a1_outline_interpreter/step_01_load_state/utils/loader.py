"""Step 01 — Load shared state written by A0."""
import json
import logging

from ...shared.models.state import A1State
from lectora_backend.pipeline.shared_utils.learning_objectives import resolve_learning_objectives

logger = logging.getLogger(__name__)


def load_shared_state(state: A1State) -> A1State:
    logger.info("[A1] Loading A0 shared state...")
    try:
        with open(state["shared_state_path"]) as f:
            data = json.load(f)
        resolved_los = resolve_learning_objectives(data)
        if resolved_los and not (data.get("extracted_inputs", {}) or {}).get("learning_objectives"):
            extracted_inputs = dict(data.get("extracted_inputs", {}) or {})
            extracted_inputs["learning_objectives"] = resolved_los
            data = {**data, "extracted_inputs": extracted_inputs}
            logger.info(
                "[A1] Backfilled %s learning objective(s) from llm_to_outline for PDF-only source.",
                len(resolved_los),
            )
        return {
            **state,
            "run_id": data["run_id"],
            "a0_data": data,
            "status": "running",
            "error": None,
        }
    except Exception as e:
        return {**state, "status": "failed", "error": f"load_shared_state: {e}"}
