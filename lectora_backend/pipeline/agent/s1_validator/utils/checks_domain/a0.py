from __future__ import annotations


def check_a0_metadata(shared_state: dict) -> list[dict]:
    """Verify A0 extracted all required metadata fields."""
    issues = []
    extracted = shared_state.get("extracted_inputs", {})

    title = extracted.get("title", "")
    if not title or title == "Unknown":
        issues.append({
            "field": "title",
            "expected": "non-empty course title",
            "found": repr(title),
            "severity": "blocker",
            "message": "A course title could not be found in the uploaded document. A title is required to continue.",
            "rule_source": "A0 metadata extraction",
        })

    course_id = extracted.get("course_id")
    if not course_id:
        issues.append({
            "field": "course_id",
            "expected": "numeric course ID",
            "found": repr(course_id),
            "severity": "warning",
            "message": "No course ID was found in the document. A default ID may be assigned — please verify this is correct.",
            "rule_source": "A0 metadata extraction",
        })

    los = extracted.get("learning_objectives", [])
    if not los:
        issues.append({
            "field": "learning_objectives",
            "expected": ">= 1 learning objective",
            "found": "0",
            "severity": "blocker",
            "message": "No learning objectives were found in the document. Learning objectives are required to build the course outline.",
            "rule_source": "content_rules.must_map_to_learning_objectives",
        })

    sample = extracted.get("content_sample", "")
    if len(sample) < 200:
        issues.append({
            "field": "content_sample",
            "expected": ">= 200 chars",
            "found": f"{len(sample)} chars",
            "severity": "warning",
            "message": "Very little text was extracted from the document. The system may not correctly identify the course type — classification results should be reviewed.",
            "rule_source": "A0 classification quality",
        })

    return issues


def check_a0_classification(shared_state: dict) -> list[dict]:
    """Verify LLM classification confidence and rule pack resolution."""
    issues = []
    llm = shared_state.get("llm_classification", {})
    request_spec = shared_state.get("request_spec", {})

    confidence = llm.get("confidence", 0)
    if confidence < 0.7:
        issues.append({
            "field": "llm_confidence",
            "expected": ">= 0.7",
            "found": confidence,
            "severity": "warning",
            "message": (
                f"The system isn't confident about what type of course this is (confidence score: {confidence}). "
                "The wrong compliance rules may have been applied — a manual review is recommended."
            ),
            "rule_source": "A0 classification",
        })

    rule_class = request_spec.get("rule_classification", {})
    if not rule_class.get("rule_pack_id"):
        issues.append({
            "field": "rule_pack_id",
            "expected": "resolved rule pack ID",
            "found": "None",
            "severity": "blocker",
            "message": "The system could not determine which compliance rules apply to this course. The process cannot continue without this information.",
            "rule_source": "A0 rule resolution",
        })

    return issues


def check_a0_timed_outline_required(shared_state: dict, rule_pack: dict) -> list[dict]:
    """If timed outline is required, ensure A0 produced TO-outline artefact."""
    issues = []
    if not rule_pack.get("content_rules", {}).get("require_timed_outline"):
        return issues

    to_outline = shared_state.get("llm_to_outline_classification")
    if not to_outline:
        issues.append(
            {
                "field": "llm_to_outline_classification",
                "expected": "present (timed outline required)",
                "found": "missing",
                "severity": "blocker",
                "message": "This course type requires a Timed Outline document, but none was found. Please ensure the Timed Outline was uploaded correctly.",
                "rule_source": "content_rules.require_timed_outline",
            }
        )
        return issues
    if to_outline.get("_no_timed_outline_doc"):
        issues.append(
            {
                "field": "llm_to_outline_classification",
                "expected": "timed outline from uploaded TO document",
                "found": "synthetic outline (no TO .docx provided)",
                "severity": "blocker",
                "message": "This course type requires a Timed Outline document, but none was found. Please ensure the Timed Outline was uploaded correctly.",
                "rule_source": "content_rules.require_timed_outline",
            }
        )
    return issues


def check_a0_images(shared_state: dict) -> list[dict]:
    """Verify image extraction results."""
    issues = []
    images = shared_state.get("images", [])

    if not images:
        issues.append({
            "field": "images",
            "expected": ">= 0",
            "found": "0",
            "severity": "info",
            "message": "No images were found in the uploaded document. Images are not required — this is just a note.",
            "rule_source": "A0 image extraction",
        })

    return issues
