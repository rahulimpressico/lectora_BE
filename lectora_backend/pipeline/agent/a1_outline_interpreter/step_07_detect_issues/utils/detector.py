"""Step 07 — Detect structural inconsistencies in course_spec."""
import logging
from ...shared.models.state import A1State

logger = logging.getLogger(__name__)


def detect_inconsistencies(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Checking for inconsistencies...")
    issues = []
    spec = state["course_spec"]
    rules = {}
    los = state["a0_data"].get("extracted_inputs", {}).get("learning_objectives", [])

    kc_found = state["kc_count"]
    min_kc = rules.get("min_kc_total", 0)
    if kc_found < min_kc:
        issues.append({
            "field": "knowledge_check_count",
            "expected": f">= {min_kc}",
            "found": kc_found,
            "severity": "warning",
            "message": (
                f"Only {kc_found} knowledge check heading(s) found; "
                f"rule pack requires at least {min_kc}."
            ),
        })

    mapped = set()
    for s in spec.get("sections", []):
        mapped.update(s.get("maps_to_objectives", []))
    unmapped = [i for i in range(len(los)) if i not in mapped]
    if unmapped:
        issues.append({
            "field": "learning_objectives_coverage",
            "expected": f"all {len(los)} LOs mapped",
            "found": f"LO indices {unmapped} unmapped",
            "severity": "info",
            "message": (
                f"LO(s) {[i+1 for i in unmapped]} have no explicit section mapping. "
                "May need A2 to address coverage gaps."
            ),
        })

    if issues:
        for iss in issues:
            logger.info("  [%s] %s: %s", iss["severity"].upper(), iss["field"], iss["message"])
    else:
        logger.info("[A1] No inconsistencies detected.")

    return {**state, "inconsistencies": issues}
