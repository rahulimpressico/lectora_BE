"""
A0 — Request Synthesizer & Input Normalizer

Accepts one or more source .docx / .pdf files and an optional Timed Outline.

Scenario 1 — TO provided:
  Parses the uploaded TO (DOCX/PDF) via LLM into structured outline JSON.

Scenario 2 — NO TO provided:
  Extracts structured content from source files (headings + para indices for DOCX,
  TOC entries + section content for PDF) and sends only the structured data to the
  LLM using GENERATE_TO_PROMPT.  No raw file upload to the Files API.

In both scenarios A0 runs rule-family classification and writes shared_state plus
llm_to_outline.json for downstream agents.
"""

import json
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ..step_01_document_parsing.utils.doc_parser import CourseDocParser
from ..step_01_document_parsing.utils.pdf_parser import PDFSourceParser
from ..step_02_classification.utils.classifier import (
    classify_with_llm,
    resolve_value,
)
from ..step_02_classification.constants.prompts import (
    DEFAULT_TO_DURATION_HOURS,
    compute_calculated_word_count,
)
from ..step_03_to_processing.utils.to_processor import (
    classify_to_outline_with_llm,
    generate_to_with_llm,
    map_to_to_source_indices,
)
from ..step_04_post_processing.utils.outline_metrics import enrich_outline_metrics
from ..step_04_post_processing.utils.title_cleaner import clean_outline_titles
from ..step_04_post_processing.utils.normalize_to_hierarchy import normalize_to_hierarchy
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
from lectora_backend.pipeline.shared_utils.learning_objectives import (
    normalize_learning_objectives,
)

logger = logging.getLogger(__name__)

# Generic single-word titles that are too vague to use as a course title.
_GENERIC_TITLES: frozenset[str] = frozenset({
    "", "course", "untitled", "document", "module", "lesson", "training",
    "presentation", "content", "material", "study", "guide",
})


def _clean_title_list(titles: list[str]) -> list[str]:
    """Deduplicate and normalise a list of candidate course titles.

    Steps:
    1. Strip whitespace from every entry.
    2. Remove empty strings.
    3. Remove entries whose entire lowercased value is in ``_GENERIC_TITLES``
       (single-word placeholders that carry no meaningful information).
    4. Deduplicate while preserving first-seen order.

    Returns the cleaned list; may be empty if all titles were generic.
    """
    seen: dict[str, None] = {}
    for raw in titles:
        t = raw.strip()
        if not t:
            continue
        if t.lower() in _GENERIC_TITLES:
            continue
        seen[t] = None
    return list(seen)


def _build_heading_map_from_heading_tree(
    heading_tree: list[dict],
) -> list[tuple[int, str, int, str]]:
    """Convert heading_tree entries into heading_map tuples."""
    return [
        (
            int(entry["para_idx"]),
            str(entry["text"]),
            int(entry["level"]),
            str(entry.get("source") or "source"),
        )
        for entry in heading_tree
        if entry.get("para_idx") is not None and entry.get("text")
    ]



class A0RequestSynthesizer:
    """
    A0 — Request Synthesizer & Input Normalizer

    All source docs (`docx_paths` / `pdf_paths`): metadata, headings, indexed
    paragraphs, images, and rule-family classification are extracted in code.

    Timed-outline doc (`to_outline_doc_path`, optional):
      - If provided: parsed via LLM into structured outline JSON (Scenario 1).
      - If omitted: TO is generated from uploaded source files via LLM (Scenario 2).

    Outputs: request_spec, provenance_log, shared_state, and llm_to_outline JSON
    files written under `output_dir/{course_slug}/`.
    """

    def __init__(
        self,
        docx_paths: Optional[list[str]] = None,
        pdf_paths: Optional[list[str]] = None,
        to_outline_doc_path: Optional[str] = None,
        output_dir: str = "shared_state",
        course_difficulty: str = "intermediate",
        extra_text_contents: Optional[list[str]] = None,
        custom_to_prompt: Optional[str] = None,
        course_type_hint: Optional[str] = None,
        audience: Optional[str] = None,
        step_logger: Optional[Callable[[str, str, str | None], None]] = None,
        *,
        docx_path: Optional[str] = None,
        extra_docx_paths: Optional[list[str]] = None,
        course_output_slug: Optional[str] = None,
        duration_hours: Optional[float] = None,
        difficulty_level: Optional[str] = None,
        calculated_word_count: Optional[int] = None,
        course_description: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        paths: list[str] = [str(p) for p in (docx_paths or []) if p]
        pdfs: list[str] = [str(p) for p in (pdf_paths or []) if p]
        if not paths and docx_path:
            paths = [str(docx_path)]
            paths.extend(str(p) for p in (extra_docx_paths or []) if p)
        if not paths and not pdfs:
            raise ValueError("At least one docx or pdf path is required")
        self.docx_paths = paths
        self.pdf_paths = pdfs
        self.docx_path = paths[0] if paths else pdfs[0]
        self.to_outline_doc_path = to_outline_doc_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.course_difficulty = (course_difficulty or "intermediate").strip().lower()
        self.run_id = str(uuid.uuid4())[:8]
        self.extra_text_contents: list[str] = extra_text_contents or []
        self.custom_to_prompt: Optional[str] = custom_to_prompt
        self.course_type_hint: Optional[str] = course_type_hint
        self.audience: Optional[str] = (audience or "").strip() or None
        self.course_description: Optional[str] = (course_description or "").strip() or None
        self.course_output_slug = (course_output_slug or "").strip() or None
        self.step_logger = step_logger

        self.difficulty_level: str = (
            (difficulty_level or course_difficulty or "intermediate").strip().lower()
        )
        self._generate_to_from_source: bool = not bool(to_outline_doc_path)
        if self._generate_to_from_source:
            self.duration_hours: float = (
                float(duration_hours)
                if duration_hours is not None
                else float(DEFAULT_TO_DURATION_HOURS)
            )
            self.calculated_word_count: int = (
                int(calculated_word_count)
                if calculated_word_count is not None
                else compute_calculated_word_count(
                    self.duration_hours, self.difficulty_level
                )
            )
        else:
            self.duration_hours = float(duration_hours) if duration_hours is not None else None
            self.calculated_word_count = (
                int(calculated_word_count) if calculated_word_count is not None else None
            )

        self.cancel_event: Optional[threading.Event] = cancel_event

    def _emit_step(self, message: str, *, level: str = "info", stage: str = "A0") -> None:
        if self.step_logger:
            self.step_logger(level, message, stage)

    def _check_cancelled(self) -> None:
        """Raise RuntimeError('Cancelled') if the cancel event has been set."""
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Cancelled")

    def _resolve_trace_doc_name(self) -> str:
        """Filesystem-safe doc stem used for LLM trace attribution (costing)."""
        if self.course_output_slug:
            return self.course_output_slug
        if len(self.docx_paths) == 1:
            return Path(self.docx_paths[0]).stem
        if len(self.pdf_paths) == 1 and not self.docx_paths:
            return Path(self.pdf_paths[0]).stem
        return f"multi_{self.run_id}"

    def _ensure_trace_context(self) -> None:
        """Tag LLM traces with doc_name/run_id when the API layer did not set them."""
        from lectora_backend.pipeline.shared_llm_config.tracer import (
            get_doc_name,
            get_run_id,
            set_doc_name,
            set_run_context,
            set_run_id,
        )

        doc_name = self._resolve_trace_doc_name()
        has_doc = bool((get_doc_name() or "").strip())
        has_run = bool((get_run_id() or "").strip())
        if not has_doc and not has_run:
            set_run_context(self.run_id, doc_name)
        elif not has_doc:
            set_doc_name(doc_name)
        elif not has_run:
            set_run_id(self.run_id)

    def run(self) -> A0Result:
        """Execute the full A0 pipeline and return a typed A0Result."""

        self._ensure_trace_context()

        has_pdf_text = bool(self.extra_text_contents)
        self._emit_step("Loading source documents and extracting structure…")
        logger.info(
            "[A0] Parsing %s DOCX source(s), %s PDF source(s)%s...",
            len(self.docx_paths),
            len(self.pdf_paths),
            f" + {len(self.extra_text_contents)} PDF text block(s)" if has_pdf_text else "",
        )

        _to_is_json = (
            self.to_outline_doc_path is not None
            and self.to_outline_doc_path.lower().endswith(".json")
        )
        _to_is_pdf = (
            self.to_outline_doc_path is not None
            and self.to_outline_doc_path.lower().endswith(".pdf")
        )
        parser = (
            CourseDocParser(
                docx_paths=self.docx_paths,
                to_outline_doc_path=None if (_to_is_json or _to_is_pdf) else self.to_outline_doc_path,
            )
            if self.docx_paths
            else None
        )
        pdf_parser = PDFSourceParser(self.pdf_paths) if self.pdf_paths else None
        to_outline_pdf_parser = (
            PDFSourceParser([self.to_outline_doc_path]) if _to_is_pdf and self.to_outline_doc_path else None
        )

        title = (
            (parser.extract_title() if parser else "")
            or (pdf_parser.extract_title() if pdf_parser else "")
            or "Course"
        )
        course_id = (
            (parser.extract_course_id() if parser else None)
            or (pdf_parser.extract_course_id() if pdf_parser else None)
        )

        learning_objectives: list[str] = []
        seen_objectives: set[str] = set()
        for items in (
            parser.extract_merged_learning_objectives() if parser else [],
            pdf_parser.extract_merged_learning_objectives() if pdf_parser else [],
        ):
            for obj in items:
                key = obj.lower()
                if key not in seen_objectives:
                    learning_objectives.append(obj)
                    seen_objectives.add(key)

        content_parts: list[str] = []
        if parser:
            docx_content = parser.extract_merged_full_content(max_words=8000)
            if docx_content:
                content_parts.append(docx_content)
        if pdf_parser:
            pdf_content = pdf_parser.extract_merged_full_content(max_words=8000)
            if pdf_content:
                content_parts.append(pdf_content)
        content_sample = "\n\n".join(part for part in content_parts if part.strip())

        if self.extra_text_contents:
            pdf_combined = "\n\n".join(t for t in self.extra_text_contents if t.strip())
            if pdf_combined:
                content_sample = content_sample + "\n\n" + pdf_combined if content_sample else pdf_combined
                logger.info(
                    "[A0] Merged %s PDF text block(s) into combined content.",
                    len(self.extra_text_contents),
                )

        classification_parts: list[str] = []
        if parser:
            sample = parser.extract_content_sample(max_chars=3000)
            if sample:
                classification_parts.append(sample)
        if pdf_parser:
            sample = pdf_parser.extract_content_sample(max_chars=3000)
            if sample:
                classification_parts.append(sample)
        classification_sample = "\n\n".join(classification_parts)

        # Only extract the TO outline text when a TO document is actually provided
        # (Scenario 1). For Scenario 2 (no TO) this extraction is skipped entirely.
        to_outline_content = ""
        if self.to_outline_doc_path and not _to_is_json:
            to_outline_content = (
                to_outline_pdf_parser.extract_to_outline_text()
                if to_outline_pdf_parser
                else (parser.extract_to_outline_text() if parser else "")
            )
            to_word_count = len(to_outline_content.split())
            logger.info(
                "[A0] TO document extracted: %d words from %s",
                to_word_count,
                Path(self.to_outline_doc_path).name,
            )
            if not to_outline_content.strip():
                logger.warning(
                    "[A0] WARNING — TO document %r extracted to empty string. "
                    "The file may use text boxes, SmartArt, or non-paragraph content "
                    "that python-docx cannot read. LLM will receive no TO content.",
                    Path(self.to_outline_doc_path).name,
                )

        total_doc_word_count = (
            (parser.count_total_doc_words() if parser else 0)
            + (pdf_parser.count_total_doc_words() if pdf_parser else 0)
        )
        logger.info("[A0] Source doc word count: %s", total_doc_word_count)

        indexed_parts: list[str] = []
        _docx_indexed_words = 0
        _pdf_indexed_words = 0
        if parser:
            indexed = parser.extract_indexed_content(max_words=None)
            if indexed:
                _docx_indexed_words = len(indexed.split())
                indexed_parts.append(indexed)
        if pdf_parser:
            indexed = pdf_parser.extract_indexed_content(max_words=None)
            if indexed:
                _pdf_indexed_words = len(indexed.split())
                indexed_parts.append(indexed)
        indexed_content = "\n".join(indexed_parts)

        total_paragraphs = 0
        if parser:
            total_paragraphs += parser.count_paragraphs()
        if pdf_parser:
            total_paragraphs += pdf_parser.count_paragraphs()

        # ── Branch detection: log which pipeline path will be taken ─────────────
        if _to_is_json:
            logger.info(
                "[TO MODE] Existing TO detected (pre-generated JSON: %s) — "
                "will load directly from disk, no LLM TO call needed.",
                self.to_outline_doc_path,
            )
        elif self.to_outline_doc_path:
            logger.info(
                "[TO MODE] Existing TO detected (%s) — continuing with detected TO.",
                Path(self.to_outline_doc_path).name,
            )
        else:
            logger.info(
                "[STRUCTURED CONTENT MODE] TO not found — extracted headings and indexed "
                "content will be sent to LLM (DOCX: heading_tree + indexed paragraphs; "
                "PDF: TOC entries + section content) "
                "(duration=%sh, difficulty=%s, target_words=%d).",
                self.duration_hours,
                self.difficulty_level,
                self.calculated_word_count,
            )

        heading_map: list = []
        if parser:
            heading_map.extend(parser.get_section_heading_map())
        pdf_heading_tree = (
            pdf_parser.extract_merged_heading_tree() if pdf_parser else []
        )
        if pdf_parser:
            heading_map.extend(_build_heading_map_from_heading_tree(pdf_heading_tree))

        logger.info("[A0] Heading anchors across sources: %s", len(heading_map))

        heading_tree: list[dict] = []
        seen_headings: set[tuple[str, str]] = set()
        for tree in [
            *(parser.extract_merged_heading_tree() if parser else []),
            *pdf_heading_tree,
        ]:
            source = str(tree.get("source") or "")
            key = (source, str(tree["text"]).lower())
            if key in seen_headings:
                continue
            heading_tree.append(tree)
            seen_headings.add(key)
        logger.info("[A0] Heading tree entries: %s", len(heading_tree))

        # ── Detailed extraction log — always emitted regardless of TO path ──────
        logger.info("[EXTRACT] ══════════════ SOURCE EXTRACTION SUMMARY ══════════════")
        if parser:
            docx_headings = [h for h in heading_tree
                             if not str(h.get("source", "")).lower().endswith(".pdf")]
            logger.info(
                "[EXTRACT]  DOCX  → %d headings extracted | %d words indexed",
                len(docx_headings),
                _docx_indexed_words,
            )
            if docx_headings:
                logger.info("[EXTRACT]  ── DOCX titles ──────────────────────────────────")
                for h in docx_headings:
                    indent = "  " * max(0, int(h.get("level", 1)) - 1)
                    logger.info(
                        "[EXTRACT]     [L%s] %s%s",
                        h.get("level", "?"),
                        indent,
                        h.get("text", ""),
                    )
        else:
            logger.info("[EXTRACT]  DOCX  → (not provided)")

        if pdf_parser:
            pdf_headings = [h for h in heading_tree
                            if str(h.get("source", "")).lower().endswith(".pdf")]
            if not pdf_headings:
                pdf_headings = pdf_heading_tree
            logger.info(
                "[EXTRACT]  PDF   → %d headings/TOC entries | %d words indexed",
                len(pdf_headings),
                _pdf_indexed_words,
            )
            if pdf_headings:
                logger.info("[EXTRACT]  ── PDF TOC / headings ─────────────────────────────")
                for h in pdf_headings:
                    indent = "  " * max(0, int(h.get("level", 1)) - 1)
                    logger.info(
                        "[EXTRACT]     [L%s] %s%s",
                        h.get("level", "?"),
                        indent,
                        h.get("text", ""),
                    )
        else:
            logger.info("[EXTRACT]  PDF   → (not provided)")

        total_indexed = _docx_indexed_words + _pdf_indexed_words
        logger.info(
            "[EXTRACT]  COMBINED → %d total words from %s source(s) "
            "(DOCX: %d | PDF: %d)",
            total_indexed,
            (1 if parser else 0) + (1 if pdf_parser else 0),
            _docx_indexed_words,
            _pdf_indexed_words,
        )
        logger.info("[EXTRACT] ══════════════════════════════════════════════════════════")

        self._check_cancelled()
        self._emit_step("Extracting source images and preparing prompts…")
        logger.info("[A0] Extracting images...")
        if self.course_output_slug:
            stem = self.course_output_slug
        elif len(self.docx_paths) == 1:
            stem = Path(self.docx_paths[0]).stem
        elif len(self.pdf_paths) == 1 and not self.docx_paths:
            stem = Path(self.pdf_paths[0]).stem
        else:
            stem = f"multi_{self.run_id}"
        doc_dir = self.output_dir / stem
        doc_dir.mkdir(parents=True, exist_ok=True)
        input_docs_dir = doc_dir / "doc"
        input_docs_dir.mkdir(parents=True, exist_ok=True)

        persisted_inputs: list[str] = []
        for src_path in [*self.docx_paths, *self.pdf_paths]:
            src = Path(src_path)
            if not src.exists() or not src.is_file():
                continue
            dest = input_docs_dir / src.name
            shutil.copy2(src, dest)
            persisted_inputs.append(dest.name)

        if self.to_outline_doc_path and not _to_is_json:
            to_src = Path(self.to_outline_doc_path)
            if to_src.exists() and to_src.is_file():
                dest = input_docs_dir / to_src.name
                shutil.copy2(to_src, dest)
                persisted_inputs.append(dest.name)

        if persisted_inputs:
            logger.info(
                "[A0] Persisted %s input file(s) -> %s",
                len(persisted_inputs),
                input_docs_dir,
            )

        images_dir = doc_dir / "images"
        images: list[dict] = []
        if parser:
            images.extend(parser.extract_all_images(images_dir))
        if pdf_parser:
            images.extend(
                pdf_parser.extract_all_images(
                    images_dir,
                    start_seq=len(images),
                    heading_anchors=pdf_heading_tree if pdf_heading_tree else None,
                )
            )
        logger.info("[A0] Extracted %s images -> %s", len(images), images_dir)

        # ── Build multi-doc title list for classification (extract from each source) ─
        _raw_titles: list[str] = []
        if parser:
            for _dp in self.docx_paths:
                try:
                    _ip = CourseDocParser(docx_paths=[str(_dp)])
                    _t = _ip.extract_title()
                    if _t and _t.strip():
                        _raw_titles.append(_t.strip())
                except Exception:
                    pass
        if pdf_parser:
            for _pp in self.pdf_paths:
                try:
                    _ip2 = PDFSourceParser([str(_pp)])
                    _t = _ip2.extract_title()
                    if _t and _t.strip():
                        _raw_titles.append(_t.strip())
                except Exception:
                    pass
        if not _raw_titles and title:
            _raw_titles = [title]

        # Deduplicate and remove generic placeholders so the LLM receives only
        # meaningful, distinct titles.
        _classify_all_titles = _clean_title_list(_raw_titles)
        if not _classify_all_titles and title:
            # Fallback: even if title looks generic, keep it so the prompt isn't empty.
            _classify_all_titles = [title]
        logger.info(
            "[A0] Classification titles (cleaned, %d of %d raw): %s",
            len(_classify_all_titles),
            len(_raw_titles),
            _classify_all_titles,
        )

        # If the primary title extracted earlier was generic or empty, promote
        # the first deduplicated title to avoid sending a placeholder to the LLM.
        if (not title or title.lower() in _GENERIC_TITLES) and _classify_all_titles:
            title = _classify_all_titles[0]
            logger.info("[A0] Primary title upgraded to: %r", title)

        # ── Build richer classification content sample (larger than default 3000 chars) ─
        _classify_parts: list[str] = []
        if parser:
            _s = parser.extract_content_sample(max_chars=8000)
            if _s:
                _classify_parts.append(_s)
        if pdf_parser:
            _s = pdf_parser.extract_content_sample(max_chars=8000)
            if _s:
                _classify_parts.append(_s)
        _rich_classification_sample = "\n\n".join(_classify_parts) or classification_sample

        self._check_cancelled()
        hints_arg = None
        t_llm = time.perf_counter()
        self._emit_step("Running rule-family classification and TO generation…")
        logger.info("[A0] Starting parallel LLM calls (classify + TO)...")

        _to_is_pregenerated_json = (
            self.to_outline_doc_path is not None
            and self.to_outline_doc_path.lower().endswith(".json")
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            classify_future = pool.submit(
                classify_with_llm,
                title,
                learning_objectives,
                _rich_classification_sample,
                all_doc_titles=_classify_all_titles or None,
                heading_tree=heading_tree or None,
                validation_hints=hints_arg,
            )

            if _to_is_pregenerated_json:
                # ── TO MODE: pre-generated JSON — load from disk, no LLM needed ──
                logger.info(
                    "[TO MODE] Existing TO detected — loading pre-generated JSON from disk."
                )

                def _load_pregenerated_to():
                    with open(self.to_outline_doc_path, encoding="utf-8") as fh:  # type: ignore[arg-type]
                        payload = json.load(fh)
                    return payload.get("llm_to_outline") or payload

                to_future = pool.submit(_load_pregenerated_to)

            elif self.to_outline_doc_path:
                # ── TO MODE: TO document provided → parse via LLM ───────────────
                logger.info(
                    "[TO MODE] Existing TO detected — sending TO document to LLM for parsing."
                )
                to_future = pool.submit(
                    classify_to_outline_with_llm,
                    to_outline_content,
                    validation_hints=hints_arg,
                )

            else:
                # ── STRUCTURED CONTENT MODE: send extracted headings/TOC to LLM ──
                # Priority order for FORMAT selection:
                #   1. DOCX with Word TOC (TOC 1/2/3 styles): FORMAT A — TOC hierarchy
                #      + per-section snippets (pura body nahi, sirf TOC + capped content).
                #   2. PDF-only with bookmarks/headings:       FORMAT A — same.
                #   3. DOCX/PDF without any TOC:               FORMAT B — heading_tree
                #      + full [P<N>]-prefixed indexed body (existing behaviour).
                def _generate_to_from_structured(
                    _title=title,
                    _objectives=learning_objectives,
                    _indexed=indexed_content,
                    _htree=heading_tree,
                ):
                    _pdf_toc_outline = None
                    if pdf_parser and parser:
                        _pdf_entries = pdf_parser.extract_toc_entries(
                            include_heading_fallback=True
                        )
                        if _pdf_entries:
                            _outline_lines = [
                                "## PDF SOURCE OUTLINE (bookmarks — structure from PDF; "
                                "body text includes DOCX + PDF below)"
                            ]
                            for _entry in _pdf_entries[:200]:
                                _page = f" p{_entry.page}" if _entry.page else ""
                                _indent = "  " * max(0, _entry.level - 1)
                                _outline_lines.append(
                                    f"{_indent}[L{_entry.level}] {_entry.text}{_page}"
                                )
                            _pdf_toc_outline = "\n".join(_outline_lines)
                            logger.info(
                                "[A0] Mixed sources: attached PDF bookmark outline "
                                "(%d entries, %d lines in prompt)",
                                len(_pdf_entries),
                                len(_outline_lines) - 1,
                            )

                    _toc_section_contents = None

                    # ── DOCX TOC path (FORMAT A — preferred when Word TOC present) ──
                    if parser:
                        _docx_toc = parser.extract_toc_entries()
                        if _docx_toc:
                            _toc_budget = min(16_000, max(8_000, 40 * len(_docx_toc)))
                            _toc_section_contents = parser.extract_toc_section_contents(
                                _docx_toc, total_word_budget=_toc_budget
                            )
                            logger.info(
                                "[A0] DOCX TOC: %d entries → FORMAT A "
                                "(TOC hierarchy + section snippets; full body skipped)",
                                len(_docx_toc),
                            )
                        else:
                            logger.info(
                                "[A0] DOCX: no Word TOC paragraphs (TOC 1/2/3 styles) found "
                                "→ FORMAT B (heading_tree + full body)"
                            )

                    # ── PDF-only TOC path (FORMAT A — only when no DOCX TOC found) ──
                    if not _toc_section_contents and pdf_parser and not parser:
                        # PDF-only: bookmark TOC + per-section snippets (FORMAT A)
                        _pdf_toc = pdf_parser.extract_toc_entries(
                            include_heading_fallback=True
                        )
                        if _pdf_toc:
                            _toc_budget = min(
                                16_000,
                                max(8_000, 40 * len(_pdf_toc)),
                            )
                            _toc_section_contents = pdf_parser.extract_toc_section_contents(
                                _pdf_toc, total_word_budget=_toc_budget
                            )
                    # Collect titles from each individual source file for multi-doc title synthesis
                    _all_doc_titles: list[str] = []
                    for _dp in self.docx_paths:
                        try:
                            _ip = CourseDocParser(docx_paths=[str(_dp)])
                            _t = _ip.extract_title()
                            if _t:
                                _all_doc_titles.append(_t)
                        except Exception:
                            pass
                    for _pp in self.pdf_paths:
                        try:
                            _ip2 = PDFSourceParser([str(_pp)])
                            _t = _ip2.extract_title()
                            if _t:
                                _all_doc_titles.append(_t)
                        except Exception:
                            pass
                    self._check_cancelled()
                    return generate_to_with_llm(
                        _title,
                        _objectives,
                        # When FORMAT A (TOC) is active, full body is not sent to LLM.
                        # Pass empty string so classifier.py logs show 0 indexed words.
                        "" if _toc_section_contents else _indexed,
                        heading_tree=_htree,
                        pdf_toc_outline=_pdf_toc_outline,
                        toc_section_contents=_toc_section_contents,
                        course_difficulty=self.difficulty_level,
                        course_type_hint=self.course_type_hint,
                        duration_hours=self.duration_hours,
                        calculated_word_count=self.calculated_word_count,
                        audience=self.audience,
                        course_description=self.course_description,
                        custom_system_prompt=self.custom_to_prompt,
                        validation_hints=hints_arg,
                        all_doc_titles=_all_doc_titles,
                    )

                to_future = pool.submit(_generate_to_from_structured)

            llm_result = classify_future.result()
            llm_to_outline_result = to_future.result()

        self._check_cancelled()
        logger.info(
            "[A0] Parallel LLM calls finished in %.1fs",
            time.perf_counter() - t_llm,
        )

        paragraphs_by_source: dict[str, int] = {}
        if parser:
            paragraphs_by_source.update(
                {path.name: len(doc.paragraphs) for path, doc in parser._sources}
            )
        if pdf_parser:
            paragraphs_by_source.update(pdf_parser.paragraphs_by_source())

        if _to_is_pregenerated_json:
            logger.info(
                "[TO MODE] Pre-generated TO loaded from disk — no LLM TO generation was performed."
            )
            llm_to_outline_result["_reused_from_preview"] = True
        elif self.to_outline_doc_path:
            # ── TO MODE completed: map TO sections → source paragraph indices ────
            raw_sections = (llm_to_outline_result or {}).get("sections") or []
            logger.info(
                "[TO MODE] Continuing with detected TO — mapping %d TO section(s) to source headings.",
                len(raw_sections),
            )
            if raw_sections and heading_map:
                mapped_sections = map_to_to_source_indices(
                    sections=raw_sections,
                    heading_map=heading_map,
                    total_paragraphs=total_paragraphs,
                    paragraphs_by_source=paragraphs_by_source,
                )
                matched = sum(
                    1 for s in mapped_sections if s.get("para_idx_start") is not None
                )
                logger.info(
                    "[TO MODE] Matched %d/%d section(s) to source headings.",
                    matched,
                    len(mapped_sections),
                )
                llm_to_outline_result["sections"] = mapped_sections
        else:
            # ── STRUCTURED CONTENT MODE completed: tag metadata on the LLM-generated outline
            n_sections = len((llm_to_outline_result or {}).get("sections") or [])
            logger.info(
                "[STRUCTURED CONTENT MODE] LLM generated %d section(s) from extracted content "
                "(duration=%sh, difficulty=%s, target_words=%d).",
                n_sections,
                self.duration_hours,
                self.difficulty_level,
                self.calculated_word_count,
            )
            llm_to_outline_result["_generated_from_source"] = True
            llm_to_outline_result["_dynamic_flow"] = True
            llm_to_outline_result["_duration_hours"] = self.duration_hours
            llm_to_outline_result["_difficulty_level"] = self.difficulty_level
            llm_to_outline_result["_calculated_word_count"] = self.calculated_word_count

        # Backfill learning objectives from the TO (or generated outline) when the
        # study guide itself has none. This covers DOCX sources where the LOs live
        # in the uploaded TO file, not in the raw study guide document.
        if not learning_objectives:
            llm_learning_objectives = normalize_learning_objectives(
                (llm_to_outline_result or {}).get("learning_objectives", [])
            )
            if llm_learning_objectives:
                learning_objectives = llm_learning_objectives
                source_label = "TO document" if self.to_outline_doc_path else "generated TO"
                logger.info(
                    "[A0] Backfilled %s learning objective(s) from %s "
                    "(none found in study guide).",
                    len(learning_objectives),
                    source_label,
                )

        rule_family_key = llm_result["rule_family"]
        if rule_family_key not in RULE_PACKS:
            # Try partial / fuzzy match before hard-failing
            _matched = next(
                (k for k in RULE_PACKS if k in rule_family_key or rule_family_key in k),
                None,
            )
            if _matched:
                logger.warning(
                    "[A0] Unknown rule_family %r — falling back to matched key %r",
                    rule_family_key,
                    _matched,
                )
                rule_family_key = _matched
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

        resolved: dict = {}
        provenance_log: dict[str, ProvenanceEntry] = {}
        for key in resolve_keys:
            rule_defaults = (
                rule_pack.get("content_rules", {})
                if key == "words_per_credit_hour"
                else {}
            )
            val, source = resolve_value(key, {}, rule_defaults, inferred)
            resolved[key] = val
            provenance_log[key] = ProvenanceEntry(value=val, source=source)

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

        _raw_totals = (llm_to_outline_result or {}).get("totals") or {}
        try:
            to_outline_total_word_count = int(_raw_totals.get("word_count") or 0)
        except (TypeError, ValueError):
            to_outline_total_word_count = 0
        logger.info("[A0] TO outline total word count (from LLM): %s", to_outline_total_word_count)

        llm_classification = LLMClassification.model_validate(llm_result)
        heading_map_serialized: list[list] = [list(entry) for entry in heading_map]

        shared_state = SharedState(
            run_id=self.run_id,
            status="a0_completed",
            request_spec=request_spec,
            provenance_log=provenance_log,
            source_document=", ".join(
                os.path.basename(p) for p in [*self.docx_paths, *self.pdf_paths]
            ),
            extracted_inputs=ExtractedInputs(
                title=title,
                course_id=course_id,
                learning_objectives=learning_objectives,
                content_sample=content_sample,
                total_doc_word_count=total_doc_word_count,
                to_outline_total_word_count=to_outline_total_word_count,
                heading_tree=heading_tree,
                heading_map=heading_map_serialized,
                indexed_content=indexed_content,
                toc_entries=[],
                toc_section_contents=[],
                total_paragraphs=total_paragraphs,
                paragraphs_by_source=paragraphs_by_source,
            ),
            images=images,
            llm_classification=llm_classification,
            llm_to_outline_classification=llm_to_outline_result,
            agent_outputs=AgentOutputSlots(),
        )

        self._emit_step("Persisting A0 outputs and generated TO artifacts…")
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

        llm_outline_copy_path = doc_dir / "llm_to_outline_COPY.json"
        with open(llm_outline_copy_path, "w", encoding="utf-8") as f:
            json.dump(llm_to_outline_payload, f, indent=2, ensure_ascii=False, default=str)
        logger.info("[A0] llm_to_outline_COPY (original) written -> %s", llm_outline_copy_path)

        outline_inner = llm_to_outline_payload.get("llm_to_outline") or {}
        _, n_titles_cleaned = clean_outline_titles(outline_inner)
        if n_titles_cleaned:
            logger.info(
                "[A0] title_cleaner removed page references from %s title(s).",
                n_titles_cleaned,
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
            difficulty=self.difficulty_level if self._generate_to_from_source else self.course_difficulty,
        )
        if was_modified:
            logger.info("[A0] outline_metrics enricher filled in missing pacing fields.")
            llm_to_outline_payload = enriched_payload

        llm_to_outline_payload["total_doc_word_count"] = total_doc_word_count
        llm_to_outline_payload["to_outline_total_word_count"] = to_outline_total_word_count

        with open(llm_outline_path, "w", encoding="utf-8") as f:
            json.dump(llm_to_outline_payload, f, indent=2, ensure_ascii=False, default=str)

        for path, data in [
            (spec_path, request_spec.model_dump(mode="json")),
            (prov_path, prov_serializable),
            (state_path, shared_state.model_dump(mode="json")),
        ]:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        logger.info("[A0] llm_to_outline written -> %s", llm_outline_path)

        # Return the FINAL enriched+normalized outline dict, not the raw LLM
        # output — enrich_outline_metrics and normalize_to_hierarchy may have
        # produced a deep-copy with updated sections that llm_to_outline_result
        # no longer reflects after those transformations.
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
