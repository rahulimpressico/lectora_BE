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

import logging
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from ..step_02_classification.constants.prompts import (
    DEFAULT_TO_DURATION_HOURS,
    compute_calculated_word_count,
)
from .classification_phase import prepare_classification_phase
from .finalization_phase import finalize_and_persist_phase
from .parse_phase import build_paragraphs_by_source, execute_parse_phase
from .to_generation_phase import execute_to_generation_phase
from lectora_backend.pipeline.models import A0Result

logger = logging.getLogger(__name__)


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
        paths: list[str] = [str(path) for path in (docx_paths or []) if path]
        pdfs: list[str] = [str(path) for path in (pdf_paths or []) if path]
        if not paths and docx_path:
            paths = [str(docx_path)]
            paths.extend(str(path) for path in (extra_docx_paths or []) if path)
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
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Cancelled")

    def _resolve_trace_doc_name(self) -> str:
        if self.course_output_slug:
            return self.course_output_slug
        if len(self.docx_paths) == 1:
            return Path(self.docx_paths[0]).stem
        if len(self.pdf_paths) == 1 and not self.docx_paths:
            return Path(self.pdf_paths[0]).stem
        return f"multi_{self.run_id}"

    def _ensure_trace_context(self) -> None:
        from lectora_backend.pipeline.shared_llm_config.tracer import (
            get_doc_name,
            get_run_id,
            set_doc_name,
            set_run_context,
            set_run_id,
            set_source_refs,
        )

        doc_name = self._resolve_trace_doc_name()
        source_refs = [*self.docx_paths, *self.pdf_paths]
        if self.to_outline_doc_path:
            source_refs.append(self.to_outline_doc_path)
        has_doc = bool((get_doc_name() or "").strip())
        has_run = bool((get_run_id() or "").strip())
        if not has_doc and not has_run:
            set_run_context(self.run_id, doc_name, source_refs=source_refs)
        elif not has_doc:
            set_doc_name(doc_name)
            set_source_refs(source_refs)
        elif not has_run:
            set_run_id(self.run_id)
            set_source_refs(source_refs)

    def run(self) -> A0Result:
        self._ensure_trace_context()

        parsed = execute_parse_phase(self)
        classification = prepare_classification_phase(self, parsed)
        paragraphs_by_source = build_paragraphs_by_source(parsed)
        generation = execute_to_generation_phase(
            self,
            parsed,
            classification,
            paragraphs_by_source=paragraphs_by_source,
        )
        return finalize_and_persist_phase(self, parsed, classification, generation)
