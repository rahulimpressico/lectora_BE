import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..step_01_document_parsing.utils.doc_parser import CourseDocParser
from ..step_01_document_parsing.utils.pdf_parser import PDFSourceParser
from ..step_02_classification.utils.classifier import classify_with_llm
from ..step_03_to_processing.utils.to_processor import (
    classify_to_outline_with_llm,
    generate_to_with_llm,
    map_to_to_source_indices,
)
from lectora_backend.pipeline.shared_llm_config.tracer import submit_with_trace_context
from lectora_backend.pipeline.shared_utils.learning_objectives import (
    normalize_learning_objectives,
)

from .classification_phase import ClassificationPhaseResult
from .parse_phase import ParsePhaseResult

logger = logging.getLogger(__name__)


@dataclass
class ToGenerationPhaseResult:
    llm_result: dict[str, Any]
    llm_to_outline_result: dict[str, Any]
    learning_objectives: list[str]
    paragraphs_by_source: dict[str, int]


def _collect_all_doc_titles_for_generation(synth: Any) -> list[str]:
    all_doc_titles: list[str] = []
    for docx_path in synth.docx_paths:
        try:
            parser = CourseDocParser(docx_paths=[str(docx_path)])
            title = parser.extract_title()
            if title:
                all_doc_titles.append(title)
        except Exception:
            pass
    for pdf_path in synth.pdf_paths:
        try:
            parser = PDFSourceParser([str(pdf_path)])
            title = parser.extract_title()
            if title:
                all_doc_titles.append(title)
        except Exception:
            pass
    return all_doc_titles


def execute_to_generation_phase(
    synth: Any,
    parsed: ParsePhaseResult,
    classification: ClassificationPhaseResult,
    paragraphs_by_source: dict[str, int],
) -> ToGenerationPhaseResult:
    synth._check_cancelled()
    hints_arg = None
    started_at = time.perf_counter()
    synth._emit_step("Running rule-family classification and TO generation…")
    logger.info("[A0] Starting parallel LLM calls (classify + TO)...")

    to_is_pregenerated_json = (
        synth.to_outline_doc_path is not None
        and synth.to_outline_doc_path.lower().endswith(".json")
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        classify_future = submit_with_trace_context(
            pool,
            classify_with_llm,
            classification.title,
            parsed.learning_objectives,
            classification.rich_classification_sample,
            all_doc_titles=classification.all_doc_titles or None,
            heading_tree=parsed.heading_tree or None,
            validation_hints=hints_arg,
        )

        if to_is_pregenerated_json:
            logger.info(
                "[TO MODE] Existing TO detected — loading pre-generated JSON from disk."
            )

            def _load_pregenerated_to() -> dict[str, Any]:
                with open(synth.to_outline_doc_path, encoding="utf-8") as file_handle:  # type: ignore[arg-type]
                    payload = json.load(file_handle)
                return payload.get("llm_to_outline") or payload

            to_future = submit_with_trace_context(pool, _load_pregenerated_to)

        elif synth.to_outline_doc_path:
            logger.info(
                "[TO MODE] Existing TO detected — sending TO document to LLM for parsing."
            )
            to_future = submit_with_trace_context(
                pool,
                classify_to_outline_with_llm,
                parsed.to_outline_content,
                validation_hints=hints_arg,
            )

        else:

            def _generate_to_from_structured(
                title: str = classification.title,
                objectives: list[str] = parsed.learning_objectives,
                indexed_content: str = parsed.indexed_content,
                heading_tree: list[dict] = parsed.heading_tree,
            ) -> dict[str, Any]:
                pdf_toc_outline = None
                if parsed.pdf_parser and parsed.parser:
                    pdf_entries = parsed.pdf_parser.extract_toc_entries(
                        include_heading_fallback=True
                    )
                    if pdf_entries:
                        outline_lines = [
                            "## PDF SOURCE OUTLINE (bookmarks — structure from PDF; "
                            "body text includes DOCX + PDF below)"
                        ]
                        for entry in pdf_entries[:200]:
                            page = f" p{entry.page}" if entry.page else ""
                            indent = "  " * max(0, entry.level - 1)
                            outline_lines.append(
                                f"{indent}[L{entry.level}] {entry.text}{page}"
                            )
                        pdf_toc_outline = "\n".join(outline_lines)
                        logger.info(
                            "[A0] Mixed sources: attached PDF bookmark outline "
                            "(%d entries, %d lines in prompt)",
                            len(pdf_entries),
                            len(outline_lines) - 1,
                        )

                toc_section_contents = None
                if parsed.parser:
                    docx_toc = parsed.parser.extract_toc_entries()
                    if docx_toc:
                        toc_budget = min(16_000, max(8_000, 40 * len(docx_toc)))
                        toc_section_contents = parsed.parser.extract_toc_section_contents(
                            docx_toc, total_word_budget=toc_budget
                        )
                        logger.info(
                            "[A0] DOCX TOC: %d entries → FORMAT A "
                            "(TOC hierarchy + section snippets; full body skipped)",
                            len(docx_toc),
                        )
                    else:
                        logger.info(
                            "[A0] DOCX: no Word TOC paragraphs (TOC 1/2/3 styles) found "
                            "→ FORMAT B (heading_tree + full body)"
                        )

                if not toc_section_contents and parsed.pdf_parser and not parsed.parser:
                    pdf_toc = parsed.pdf_parser.extract_toc_entries(
                        include_heading_fallback=True
                    )
                    if pdf_toc:
                        toc_budget = min(
                            16_000,
                            max(8_000, 40 * len(pdf_toc)),
                        )
                        toc_section_contents = parsed.pdf_parser.extract_toc_section_contents(
                            pdf_toc, total_word_budget=toc_budget
                        )

                all_doc_titles = _collect_all_doc_titles_for_generation(synth)
                synth._check_cancelled()
                return generate_to_with_llm(
                    title,
                    objectives,
                    "" if toc_section_contents else indexed_content,
                    heading_tree=heading_tree,
                    pdf_toc_outline=pdf_toc_outline,
                    toc_section_contents=toc_section_contents,
                    course_difficulty=synth.difficulty_level,
                    course_type_hint=synth.course_type_hint,
                    duration_hours=synth.duration_hours,
                    calculated_word_count=synth.calculated_word_count,
                    audience=synth.audience,
                    course_description=synth.course_description,
                    custom_system_prompt=synth.custom_to_prompt,
                    validation_hints=hints_arg,
                    all_doc_titles=all_doc_titles,
                )

            to_future = submit_with_trace_context(pool, _generate_to_from_structured)

        llm_result = classify_future.result()
        llm_to_outline_result = to_future.result()

    synth._check_cancelled()
    logger.info(
        "[A0] Parallel LLM calls finished in %.1fs",
        time.perf_counter() - started_at,
    )

    if to_is_pregenerated_json:
        logger.info(
            "[TO MODE] Pre-generated TO loaded from disk — no LLM TO generation was performed."
        )
        llm_to_outline_result["_reused_from_preview"] = True
    elif synth.to_outline_doc_path:
        raw_sections = (llm_to_outline_result or {}).get("sections") or []
        logger.info(
            "[TO MODE] Continuing with detected TO — mapping %d TO section(s) to source headings.",
            len(raw_sections),
        )
        if raw_sections and parsed.heading_map:
            mapped_sections = map_to_to_source_indices(
                sections=raw_sections,
                heading_map=parsed.heading_map,
                total_paragraphs=parsed.total_paragraphs,
                paragraphs_by_source=paragraphs_by_source,
            )
            matched = sum(1 for section in mapped_sections if section.get("para_idx_start") is not None)
            logger.info(
                "[TO MODE] Matched %d/%d section(s) to source headings.",
                matched,
                len(mapped_sections),
            )
            llm_to_outline_result["sections"] = mapped_sections
    else:
        section_count = len((llm_to_outline_result or {}).get("sections") or [])
        logger.info(
            "[STRUCTURED CONTENT MODE] LLM generated %d section(s) from extracted content "
            "(duration=%sh, difficulty=%s, target_words=%d).",
            section_count,
            synth.duration_hours,
            synth.difficulty_level,
            synth.calculated_word_count,
        )
        llm_to_outline_result["_generated_from_source"] = True
        llm_to_outline_result["_dynamic_flow"] = True
        llm_to_outline_result["_duration_hours"] = synth.duration_hours
        llm_to_outline_result["_difficulty_level"] = synth.difficulty_level
        llm_to_outline_result["_calculated_word_count"] = synth.calculated_word_count

    learning_objectives = list(parsed.learning_objectives)
    if not learning_objectives:
        llm_learning_objectives = normalize_learning_objectives(
            (llm_to_outline_result or {}).get("learning_objectives", [])
        )
        if llm_learning_objectives:
            learning_objectives = llm_learning_objectives
            source_label = "TO document" if synth.to_outline_doc_path else "generated TO"
            logger.info(
                "[A0] Backfilled %s learning objective(s) from %s "
                "(none found in study guide).",
                len(learning_objectives),
                source_label,
            )

    return ToGenerationPhaseResult(
        llm_result=llm_result,
        llm_to_outline_result=llm_to_outline_result,
        learning_objectives=learning_objectives,
        paragraphs_by_source=paragraphs_by_source,
    )
