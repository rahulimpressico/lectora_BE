"""
A0 — Request Synthesizer & Input Normalizer

Accepts one or more source .docx files and an optional Timed Outline .docx.

Scenario 1 — TO provided:
  Uses the TO as the primary course structure. All source documents are treated
  as supporting material; their content is merged for LLM classification.

Scenario 2 — NO TO provided:
  Analyzes all source documents together and calls the LLM to generate a complete
  Timed Outline automatically from the combined content.

In both scenarios A0 runs rule-family classification, resolves assessment fields
from rule_pack_config, and writes shared_state plus sidecars including
llm_to_outline.json for downstream agents.
"""

import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .utils.doc_parser import CourseDocParser
from .utils.classifier import (
    classify_with_llm,
    resolve_value,
    classify_to_outline_with_llm,
    generate_to_with_llm,
    map_to_to_source_indices,
)
from .utils.outline_metrics import enrich_outline_metrics
from .utils.title_cleaner import clean_outline_titles
from lectora_backend.pipeline.models import (
    CourseMetadata,
    RuleClassification,
    RequestSpec,
    ExtractedInputs,
    LLMClassification,
    ProvenanceEntry,
    AgentOutputSlots,
    SharedState,
    A0Result,
    A0OutputFiles,
)
from lectora_backend.pipeline.rule_pack_config.rule_packs import (
    RULE_PACKS,
)

logger = logging.getLogger(__name__)


class A0RequestSynthesizer:
    """
    A0 — Request Synthesizer & Input Normalizer

    Primary source doc (`docx_path`): four bootstrap fields, images, and rule-family
    classification via LLM.

    Additional source docs (`extra_docx_paths`): their content is merged with the
    primary doc for LLM classification and TO generation.

    Timed-outline doc (`to_outline_doc_path`, optional):
      - If provided: parsed via LLM into structured outline JSON (Scenario 1).
      - If omitted: A0 generates a complete TO from the combined source content
        via LLM (Scenario 2). No longer falls back to a synthetic single-lesson stub.

    Outputs: request_spec, provenance_log, shared_state, and llm_to_outline JSON
    files written under `output_dir/{doc_stem}/`.
    """

    def __init__(
        self,
        docx_path: str,
        to_outline_doc_path: Optional[str] = None,
        output_dir: str = "shared_state",
        extra_docx_paths: Optional[list[str]] = None,
        course_difficulty: str = "intermediate",
    ):
        self.docx_path = docx_path
        self.extra_docx_paths = extra_docx_paths or []
        self.to_outline_doc_path = to_outline_doc_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.course_difficulty = (course_difficulty or "intermediate").strip().lower()
        self.run_id = str(uuid.uuid4())[:8]

    def run(self) -> A0Result:
        """Execute the full A0 pipeline and return a typed A0Result."""

        # -- Step 1: Extract raw inputs from primary doc (+ extra docs if provided)
        is_multi_doc = bool(self.extra_docx_paths)
        if is_multi_doc:
            logger.info(
                "[A0] Parsing %s source document(s): primary + %s extra...",
                1 + len(self.extra_docx_paths),
                len(self.extra_docx_paths),
            )
        else:
            logger.info("[A0] Parsing document...")

        # When the timed-outline path is a pre-generated JSON file (from the
        # generate-to preview step) we must NOT pass it to CourseDocParser —
        # python-docx would try to open it as a DOCX ZIP and raise
        # PackageNotFoundError.  The JSON is loaded later in the LLM section.
        _to_is_json = (
            self.to_outline_doc_path is not None
            and self.to_outline_doc_path.lower().endswith(".json")
        )
        parser = CourseDocParser(
            self.docx_path,
            None if _to_is_json else self.to_outline_doc_path,
            extra_docx_paths=self.extra_docx_paths if is_multi_doc else None,
        )
        title = parser.extract_title()
        course_id = parser.extract_course_id()

        # Merge objectives and content across all docs when multiple files provided
        if is_multi_doc:
            learning_objectives = parser.extract_merged_learning_objectives()
            content_sample = parser.extract_merged_full_content(max_words=8000)
        else:
            learning_objectives = parser.extract_learning_objectives()
            content_sample = parser.extract_full_content(max_words=8000)

        # Small sample for LLM classification (headings + first paragraphs, primary doc only)
        classification_sample = parser.extract_content_sample(max_chars=3000)
        to_outline_content = parser.extract_to_outline_text()
        total_doc_word_count = parser.count_total_doc_words()
        if is_multi_doc:
            total_doc_word_count += parser.count_extra_doc_words()
        logger.info("[A0] Source doc word count: %s", total_doc_word_count)

        # Indexed content (primary doc with [P<N>] markers) — used for para_idx mapping
        indexed_content = parser.extract_indexed_content(max_words=8000)
        heading_map = parser.get_section_heading_map()
        total_paragraphs = parser.count_paragraphs()
        logger.info("[A0] Heading anchors in primary doc: %s", len(heading_map))

        # -- Step 1b: Extract images (no LLM)
        logger.info("[A0] Extracting images...")
        stem = Path(self.docx_path).stem
        doc_dir = self.output_dir / stem
        doc_dir.mkdir(parents=True, exist_ok=True)
        images_dir = doc_dir / "images"
        images = parser.extract_images(self.docx_path, images_dir)
        logger.info("[A0] Extracted %s images -> %s", len(images), images_dir)
        # -- Step 2: LLM calls (run in parallel — two sequential o3 calls doubled wall time)
        hints_arg = None
        t_llm = time.perf_counter()
        logger.info("[A0] Starting parallel LLM calls (classify + TO)...")

        # ── Detect pre-generated TO JSON (from generate-to preview step) ──────
        # When the FE passes a .json file as timedOutline.blobPath the TO was
        # already generated by A0 during the generate-to preview — load it
        # directly to avoid a redundant LLM call and ensure the pipeline uses
        # the same TO the user reviewed/edited in the UI.
        _to_is_pregenerated_json = (
            self.to_outline_doc_path is not None
            and self.to_outline_doc_path.lower().endswith(".json")
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            classify_future = pool.submit(
                classify_with_llm,
                title,
                learning_objectives,
                classification_sample,
                validation_hints=hints_arg,
            )

            if _to_is_pregenerated_json:
                # Fast path: load pre-generated TO, no LLM call needed
                def _load_pregenerated_to():
                    with open(self.to_outline_doc_path, encoding="utf-8") as fh:  # type: ignore[arg-type]
                        payload = json.load(fh)
                    return payload.get("llm_to_outline") or payload

                to_future = pool.submit(_load_pregenerated_to)
            elif self.to_outline_doc_path:
                to_future = pool.submit(
                    classify_to_outline_with_llm,
                    to_outline_content,
                    validation_hints=hints_arg,
                )
            else:
                to_future = pool.submit(
                    generate_to_with_llm,
                    title=title,
                    objectives=learning_objectives,
                    indexed_content=indexed_content,
                    course_difficulty=self.course_difficulty,
                    validation_hints=hints_arg,
                )

            llm_result = classify_future.result()
            llm_to_outline_result = to_future.result()

        logger.info(
            "[A0] Parallel LLM calls finished in %.1fs",
            time.perf_counter() - t_llm,
        )

        if _to_is_pregenerated_json:
            # Pre-generated TO already has paragraph indices from the preview step;
            # skip re-mapping and mark as reused so downstream can log it correctly.
            logger.info("[A0] Using pre-generated TO from preview step (skipping LLM + mapping).")
            llm_to_outline_result["_reused_from_preview"] = True
        elif self.to_outline_doc_path:
            raw_sections = (llm_to_outline_result or {}).get("sections") or []
            if raw_sections and heading_map:
                logger.info(
                    "[A0] Mapping %s TO sections to source paragraph indices...",
                    len(raw_sections),
                )
                mapped_sections = map_to_to_source_indices(
                    sections=raw_sections,
                    heading_map=heading_map,
                    total_paragraphs=total_paragraphs,
                )
                matched = sum(
                    1 for s in mapped_sections if s.get("para_idx_start") is not None
                )
                logger.info(
                    "[A0] Matched %s/%s sections to source headings.",
                    matched,
                    len(mapped_sections),
                )
                llm_to_outline_result["sections"] = mapped_sections
        else:
            llm_to_outline_result["_generated_from_source"] = True

        rule_family_key = llm_result["rule_family"]
        # -- Step 3: Look up active rule pack
        if rule_family_key not in RULE_PACKS:
            raise ValueError(
                f"LLM returned unknown rule family '{rule_family_key}'. "
                f"Valid: {list(RULE_PACKS.keys())}"
            )
        rule_pack = RULE_PACKS[rule_family_key]

        # -- Step 4: Identify rule family
        family_name = rule_pack["family"]

        # -- Step 5: Build inferred values (from LLM output)
        inferred = {
            "topic": llm_result.get("topic"),
            "audience": llm_result.get("audience"),
            "course_type": llm_result.get("course_type"),
            "category": llm_result.get("category"),
        }

        # -- Step 6: Resolve all values with typed provenance
        resolve_keys = [
            "words_per_credit_hour",
            "topic",
            "audience",
            "course_type",
            "category",
        ]

        resolved: dict = {}
        provenance_log: dict[str, ProvenanceEntry] = {}
        for key in resolve_keys:
            rule_defaults = (
                rule_pack.get("content_rules", {})
                if key == "words_per_credit_hour"
                else {}
            )
            val, source = resolve_value(
                key, {}, rule_defaults, inferred
            )
            resolved[key] = val
            provenance_log[key] = ProvenanceEntry(value=val, source=source)

        # -- Step 7: Build typed RequestSpec
        request_spec = RequestSpec(
            run_id=self.run_id,
            timestamp=datetime.now(timezone.utc),
            course_metadata=CourseMetadata(
                title=title,
                course_id=course_id,
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

        # -- Step 7.5: Extract TO total word count from raw LLM result
        _raw_totals = (llm_to_outline_result or {}).get("totals") or {}
        try:
            to_outline_total_word_count = int(_raw_totals.get("word_count") or 0)
        except (TypeError, ValueError):
            to_outline_total_word_count = 0
        logger.info("[A0] TO outline total word count (from LLM): %s", to_outline_total_word_count)

        # -- Step 8: Build typed SharedState
        # LLMClassification.model_validate ignores extra keys from the raw dict
        llm_classification = LLMClassification.model_validate(llm_result)

        shared_state = SharedState(
            run_id=self.run_id,
            status="a0_completed",
            request_spec=request_spec,
            provenance_log=provenance_log,
            source_document=os.path.basename(self.docx_path),
            extracted_inputs=ExtractedInputs(
                title=title,
                course_id=course_id,
                learning_objectives=learning_objectives,
                content_sample=content_sample,
                total_doc_word_count=total_doc_word_count,
                to_outline_total_word_count=to_outline_total_word_count,
            ),
            images=images,                          # raw dicts from doc_parser
            llm_classification=llm_classification,
            llm_to_outline_classification=llm_to_outline_result,
            agent_outputs=AgentOutputSlots(),
        )

        # -- Step 9: Persist to disk using model serialization
        spec_path = doc_dir / "request_spec.json"
        prov_path = doc_dir / "provenance_log.json"
        state_path = doc_dir / "shared_state.json"
        llm_outline_path = doc_dir / "llm_to_outline.json"

        prov_serializable = {k: v.model_dump(mode="json") for k, v in provenance_log.items()}

        llm_to_outline_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "course_id": course_id,
            "llm_to_outline": llm_to_outline_result,
        }

        # ── Save unmodified copy before enrichment ────────────────────────
        llm_outline_copy_path = doc_dir / "llm_to_outline_COPY.json"
        with open(llm_outline_copy_path, "w", encoding="utf-8") as f:
            json.dump(llm_to_outline_payload, f, indent=2, ensure_ascii=False, default=str)
        logger.info("[A0] llm_to_outline_COPY (original) written -> %s", llm_outline_copy_path)

        # ── Strip "page N" / "pg N" artefacts from every title ────────────
        outline_inner = llm_to_outline_payload.get("llm_to_outline") or {}
        _, n_titles_cleaned = clean_outline_titles(outline_inner)
        if n_titles_cleaned:
            logger.info(
                "[A0] title_cleaner removed page references from %s title(s).",
                n_titles_cleaned,
            )

        # ── Enrich missing word_count / minutes / credit_hour fields ──────
        enriched_payload, was_modified = enrich_outline_metrics(llm_to_outline_payload)
        if was_modified:
            logger.info("[A0] outline_metrics enricher filled in missing pacing fields.")
            llm_to_outline_payload = enriched_payload

        # ── Embed totals in llm_to_outline.json for reference ────────────
        llm_to_outline_payload["total_doc_word_count"] = total_doc_word_count
        llm_to_outline_payload["to_outline_total_word_count"] = to_outline_total_word_count

        with open(llm_outline_path, "w", encoding="utf-8") as f:
            json.dump(llm_to_outline_payload, f, indent=2, ensure_ascii=False, default=str)

        for path, data in [
            (spec_path, request_spec.model_dump(mode="json")),
            (prov_path, prov_serializable),
            (state_path, shared_state.model_dump(mode="json")),
        ]:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)

        logger.info("[A0] llm_to_outline written -> %s", llm_outline_path)

        output_files = A0OutputFiles(
            request_spec=str(spec_path),
            provenance_log=str(prov_path),
            shared_state=str(state_path),
            llm_to_outline=str(llm_outline_path),
            llm_to_outline_raw=str(llm_outline_copy_path),
        )

        return A0Result(
            request_spec=request_spec,
            provenance_log=provenance_log,
            shared_state_path=str(state_path),
            output_files=output_files,
            llm_to_outline=llm_to_outline_result,
        )
