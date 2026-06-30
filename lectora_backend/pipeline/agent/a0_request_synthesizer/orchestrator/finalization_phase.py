import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from ..step_02_classification.utils.classifier import resolve_value
from ..step_04_post_processing.utils.normalize_to_hierarchy import normalize_to_hierarchy
from ..step_04_post_processing.utils.outline_metrics import enrich_outline_metrics
from ..step_04_post_processing.utils.title_cleaner import clean_outline_titles
from lectora_backend.pipeline.models import (
    A0OutputFiles,
    A0Result,
    AgentOutputSlots,
    CourseMetadata,
    ExtractedInputs,
    LLMClassification,
    ProvenanceEntry,
    RequestSpec,
    RuleClassification,
    SharedState,
)
from lectora_backend.pipeline.rule_pack_config.rule_packs import RULE_PACKS

from .classification_phase import ClassificationPhaseResult
from .parse_phase import ParsePhaseResult
from .to_generation_phase import ToGenerationPhaseResult

logger = logging.getLogger(__name__)


def finalize_and_persist_phase(
    synth: Any,
    parsed: ParsePhaseResult,
    classification: ClassificationPhaseResult,
    generation: ToGenerationPhaseResult,
) -> A0Result:
    llm_result = generation.llm_result
    llm_to_outline_result = generation.llm_to_outline_result

    rule_family_key = llm_result["rule_family"]
    if rule_family_key not in RULE_PACKS:
        matched = next(
            (key for key in RULE_PACKS if key in rule_family_key or rule_family_key in key),
            None,
        )
        if matched:
            logger.warning(
                "[A0] Unknown rule_family %r — falling back to matched key %r",
                rule_family_key,
                matched,
            )
            rule_family_key = matched
        else:
            logger.error(
                "[A0] Unknown rule_family %r (valid: %s) — defaulting to 'insurance_ce'",
                rule_family_key,
                list(RULE_PACKS.keys()),
            )
            rule_family_key = "insurance_ce"

    rule_pack = RULE_PACKS[rule_family_key]
    family_name = rule_pack["family"]

    inferred = {
        "topic": llm_result.get("topic"),
        "audience": llm_result.get("audience"),
        "course_type": llm_result.get("course_type"),
        "category": llm_result.get("category"),
    }

    resolve_keys = [
        "words_per_credit_hour",
        "topic",
        "audience",
        "course_type",
        "category",
    ]

    resolved: dict[str, Any] = {}
    provenance_log: dict[str, ProvenanceEntry] = {}
    for key in resolve_keys:
        rule_defaults = (
            rule_pack.get("content_rules", {})
            if key == "words_per_credit_hour"
            else {}
        )
        value, source = resolve_value(key, {}, rule_defaults, inferred)
        resolved[key] = value
        provenance_log[key] = ProvenanceEntry(value=value, source=source)

    request_spec = RequestSpec(
        run_id=synth.run_id,
        timestamp=datetime.now(timezone.utc),
        course_metadata=CourseMetadata(
            title=classification.title,
            course_id=parsed.course_id,
            audience=resolved["audience"],
            course_type=resolved["course_type"],
            category=resolved["category"],
            topic=resolved["topic"],
        ),
        rule_classification=RuleClassification(
            family=family_name,
            rule_pack_id=rule_pack["id"],
            rule_pack_version=rule_pack["version"],
            llm_confidence=llm_result.get("confidence"),
            llm_reasoning=llm_result.get("reasoning"),
        ),
    )

    raw_totals = (llm_to_outline_result or {}).get("totals") or {}
    try:
        to_outline_total_word_count = int(raw_totals.get("word_count") or 0)
    except (TypeError, ValueError):
        to_outline_total_word_count = 0
    logger.info("[A0] TO outline total word count (from LLM): %s", to_outline_total_word_count)

    llm_classification = LLMClassification.model_validate(llm_result)
    heading_map_serialized: list[list] = [list(entry) for entry in parsed.heading_map]

    shared_state = SharedState(
        run_id=synth.run_id,
        status="a0_completed",
        request_spec=request_spec,
        provenance_log=provenance_log,
        source_document=", ".join(
            os.path.basename(path) for path in [*synth.docx_paths, *synth.pdf_paths]
        ),
        extracted_inputs=ExtractedInputs(
            title=classification.title,
            course_id=parsed.course_id,
            learning_objectives=generation.learning_objectives,
            content_sample=parsed.content_sample,
            total_doc_word_count=parsed.total_doc_word_count,
            to_outline_total_word_count=to_outline_total_word_count,
            heading_tree=parsed.heading_tree,
            heading_map=heading_map_serialized,
            indexed_content=parsed.indexed_content,
            toc_entries=[],
            toc_section_contents=[],
            total_paragraphs=parsed.total_paragraphs,
            paragraphs_by_source=generation.paragraphs_by_source,
        ),
        images=parsed.images,
        llm_classification=llm_classification,
        llm_to_outline_classification=llm_to_outline_result,
        agent_outputs=AgentOutputSlots(),
    )

    synth._emit_step("Persisting A0 outputs and generated TO artifacts…")
    spec_path = parsed.doc_dir / "request_spec.json"
    prov_path = parsed.doc_dir / "provenance_log.json"
    state_path = parsed.doc_dir / "shared_state.json"
    llm_outline_path = parsed.doc_dir / "llm_to_outline.json"

    prov_serializable = {key: value.model_dump(mode="json") for key, value in provenance_log.items()}
    llm_to_outline_payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": synth.run_id,
        "course_id": parsed.course_id,
        "llm_to_outline": llm_to_outline_result,
    }

    llm_outline_copy_path = parsed.doc_dir / "llm_to_outline_COPY.json"
    with open(llm_outline_copy_path, "w", encoding="utf-8") as copy_handle:
        json.dump(llm_to_outline_payload, copy_handle, indent=2, ensure_ascii=False, default=str)
    logger.info("[A0] llm_to_outline_COPY (original) written -> %s", llm_outline_copy_path)

    outline_inner = llm_to_outline_payload.get("llm_to_outline") or {}
    _, cleaned_titles_count = clean_outline_titles(outline_inner)
    if cleaned_titles_count:
        logger.info(
            "[A0] title_cleaner removed page references from %s title(s).",
            cleaned_titles_count,
        )

    outline_inner = llm_to_outline_payload.get("llm_to_outline") or {}
    normalized_inner, hierarchy_modified = normalize_to_hierarchy(outline_inner)
    if hierarchy_modified:
        logger.warning(
            "[A0] normalize_to_hierarchy: course topics were nested under a "
            "reserved section (Overview/LO) — promoted to top-level sections "
            "and renumbered. Check llm_to_outline_COPY.json for the raw LLM output."
        )
        llm_to_outline_payload["llm_to_outline"] = normalized_inner

    enriched_payload, was_modified = enrich_outline_metrics(
        llm_to_outline_payload,
        difficulty=synth.difficulty_level if synth._generate_to_from_source else synth.course_difficulty,
    )
    if was_modified:
        logger.info("[A0] outline_metrics enricher filled in missing pacing fields.")
        llm_to_outline_payload = enriched_payload

    llm_to_outline_payload["total_doc_word_count"] = parsed.total_doc_word_count
    llm_to_outline_payload["to_outline_total_word_count"] = to_outline_total_word_count

    with open(llm_outline_path, "w", encoding="utf-8") as outline_handle:
        json.dump(llm_to_outline_payload, outline_handle, indent=2, ensure_ascii=False, default=str)

    for path, data in [
        (spec_path, request_spec.model_dump(mode="json")),
        (prov_path, prov_serializable),
        (state_path, shared_state.model_dump(mode="json")),
    ]:
        with open(path, "w", encoding="utf-8") as file_handle:
            json.dump(data, file_handle, indent=2, ensure_ascii=False, default=str)

    logger.info("[A0] llm_to_outline written -> %s", llm_outline_path)
    final_llm_to_outline = llm_to_outline_payload.get("llm_to_outline") or llm_to_outline_result

    return A0Result(
        request_spec=request_spec,
        provenance_log=provenance_log,
        shared_state_path=str(state_path),
        output_files=A0OutputFiles(
            request_spec=str(spec_path),
            provenance_log=str(prov_path),
            shared_state=str(state_path),
            llm_to_outline=str(llm_outline_path),
            llm_to_outline_raw=str(llm_outline_copy_path),
        ),
        llm_to_outline=final_llm_to_outline,
    )
