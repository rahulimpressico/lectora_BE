"""
Local dev pipeline routes — full course generation without Azure infrastructure.

Exposes the same URL surface that the production main.py provides so the
frontend can target a single /api base URL regardless of which server is running:

    POST   /jobs                        — create & start a pipeline job
    GET    /jobs/{jobId}                — poll job status + stage progress
    GET    /jobs/{jobId}/events         — SSE stream (stage_update events)
    GET    /jobs/{jobId}/course         — course content (completed jobs only)
    POST   /jobs/{jobId}/ai             — AI section operations (stub)
    GET    /jobs/{jobId}/artifacts      — artifact manifest
    GET    /jobs/{jobId}/artifacts/download — docx download

Architecture mirrors generate_to.py:
  - POST /jobs queues a background asyncio task via asyncio.to_thread
  - The sync runner (_run_pipeline_sync) calls pipeline agents directly
  - The in-memory LocalCourseJobStore tracks progress + logs
  - SSE /events streams store state to the frontend every second
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

def _resolve_and_validate(blob_path: str, label: str) -> str:
    """Resolve *blob_path* to an absolute local path, raising HTTP 422 if missing.

    Uses the shared blob_resolver which:
      1. Checks local _UPLOAD_ROOT cache (fast path).
      2. Downloads from Azure Blob Storage and persists to _UPLOAD_ROOT if
         Azure is configured and the file is not cached locally.
      3. Handles the ``uploaded-documents/`` prefix that Azure browser paths carry.

    Raises
    ------
    HTTPException 422
        When the file cannot be found locally or in Azure, with an actionable
        message telling the user to re-upload the document.
    """
    from lectora_backend.core.blob_resolver import resolve_blob_to_local

    resolved = resolve_blob_to_local(blob_path)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "file_not_found",
                "label": label,
                "blobPath": blob_path,
                "message": (
                    f"{label} could not be located: '{blob_path}'. "
                    "The file may have been uploaded in a previous session that "
                    "has since expired, or it only exists in Azure and could not "
                    "be downloaded. Please re-upload the document and try again."
                ),
            },
        )
    logger.info("[blob] %s resolved: %r → %s", label, blob_path, resolved)
    return str(resolved)

# Course-title output roots (``{slug}/``) — same layout as Azure artifacts.
_PIPELINE_COURSES_DIR = Path(__file__).resolve().parents[2] / "pipeline" / "courses"
_PIPELINE_COURSES_DIR.mkdir(parents=True, exist_ok=True)
_LEGACY_SHARED_STATE_DIR = Path(__file__).resolve().parents[2] / "pipeline" / "shared_state"
_LEGACY_SHARED_STATE_DIR.mkdir(parents=True, exist_ok=True)

from lectora_backend.api.local_course_job_store import (
    LocalJobStatus,
    get_local_course_job_store,
)
from lectora_backend.core.course_storage import sanitize_course_slug
from lectora_backend.core.job_registry import register_local_pipeline, unregister_local_pipeline
from lectora_backend.core.storage_cleanup import delete_course_output_tree
from lectora_backend.models.constants import MAX_A0_A1_S1_CYCLES, MAX_A2_S2_CYCLES
from lectora_backend.pipeline.agent.a0_request_synthesizer.main import (
    A0RequestSynthesizer,
)
from lectora_backend.pipeline.agent.a1_outline_interpreter.main import run as a1_run
from lectora_backend.pipeline.agent.a2_content_generator.main import (
    A2ContentGenerator,
    render_study_guide_from_state,
)
from lectora_backend.pipeline.agent.a2_content_generator.step_04_render_docx.utils.doc_formatter import (
    _inject_missing_lesson_parent_sections,
)
from lectora_backend.pipeline.agent.kc_planner.main import run as kc_planner_run
from lectora_backend.pipeline.agent.s1_validator.main import S1Validator
from lectora_backend.pipeline.agent.s2_validator.main import S2Validator
from lectora_backend.pipeline.agent.section_mapper.main import run as section_mapper_run
from lectora_backend.pipeline.models.validation import S1Status, S2Status

logger = logging.getLogger(__name__)
router = APIRouter()

_SSE_POLL_INTERVAL = 1.0  # seconds between SSE frames


def _paragraphs_to_text(body_paragraphs: list[dict]) -> str:
    """Convert A2 body_paragraphs list to plain readable text."""
    parts: list[str] = []
    for para in body_paragraphs or []:
        ptype = para.get("type", "")
        if ptype == "text":
            parts.append(para.get("content", "").strip())
        elif ptype == "bullet_list":
            items = para.get("items") or []
            parts.append("\n".join(f"• {item}" for item in items))
        elif ptype in ("important_callout", "callout"):
            parts.append(f"Important: {para.get('content', '').strip()}")
        elif ptype in ("heading_3", "heading_4"):
            parts.append(f"### {para.get('content', '').strip()}")
        elif ptype == "knowledge_check":
            lines = [f"Knowledge Check: {para.get('question', '')}"]
            for opt in para.get("options") or []:
                lines.append(f"  {opt}")
            if para.get("explanation"):
                lines.append(f"Answer: {para.get('explanation', '')}")
            parts.append("\n".join(lines))
    return "\n\n".join(p for p in parts if p)


def _persist_section_text(job_id: str, section_id: str, new_content: str) -> None:
    """Persist plain-text content to shared_state for any section (special or regular)."""
    store = get_local_course_job_store()
    job = store.get(job_id) or _recover_job_from_disk(job_id)
    if not job or not job.shared_state_path:
        return
    with open(job.shared_state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)
    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    if section_id == "course-overview":
        a2_output["course_description"] = new_content
    elif section_id == "course-conclusion":
        a2_output["course_conclusion"] = new_content
    else:
        sections: list[dict] = a2_output.get("sections") or []
        for sec in sections:
            if sec.get("section_id") == section_id:
                sec["body_paragraphs"] = [{"type": "text", "content": new_content}]
                sec["word_count"] = len(new_content.split())
                break
        a2_output["sections"] = sections
    shared_state["agent_outputs"]["A2"] = a2_output
    with open(job.shared_state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)


def _recover_job_from_disk(job_id: str):
    """Find and register a completed job from filesystem when it's absent from the in-memory store.

    This handles the common dev-server-restart case where the in-memory store is empty
    but pipeline/courses/{slug}/{job_id}/ still exists on disk.
    Returns the LocalCourseJob if found, or None.
    """
    # Job ID is always a path segment: pipeline/courses/{slug}/{job_id}/
    for state_file in (
        *_PIPELINE_COURSES_DIR.glob(f"*/{job_id}/shared_state.json"),
        *_PIPELINE_COURSES_DIR.glob(f"*/{job_id}/state/shared_state.json"),
    ):
        if not state_file.is_file():
            continue
        try:
            with open(state_file, encoding="utf-8") as fh:
                state = json.load(fh)
            artifact_dir = _artifact_dir_from_state_file(state_file)
            docx_candidate = _resolve_study_guide_path(artifact_dir)
            store = get_local_course_job_store()
            return store.register_from_filesystem(
                job_id=job_id,
                course_title=_course_title_from_shared_state(state, artifact_dir.parent.name),
                course_type=_course_type_from_shared_state(state),
                shared_state_path=str(state_file),
                study_guide_path=str(docx_candidate) if docx_candidate else None,
                temp_dir=str(artifact_dir),
            )
        except Exception:
            continue
    return None


def _regenerate_special_section_sync(job_id: str, section_id: str) -> str:
    """Regenerate Conclusion (LLM) or return the stored user-provided Overview verbatim."""
    from lectora_backend.pipeline.agent.a2_content_generator.main import (
        _build_course_conclusion,
    )

    store = get_local_course_job_store()
    job = store.get(job_id) or _recover_job_from_disk(job_id)
    if not job or not job.shared_state_path:
        raise ValueError(f"Job {job_id} not found or has no shared state")

    with open(job.shared_state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}

    if section_id == "course-overview":
        # The overview comes from the TO's description — the single source of truth.
        # Read from llm_to_outline_classification first, then fall back to what A2 stored.
        llm_to: dict = shared_state.get("llm_to_outline_classification") or {}
        new_content = (
            (llm_to.get("description") or "")
            or (a2_output.get("course_description") or "")
        ).strip()
        if not new_content:
            raise ValueError("No course description found in TO or A2 output.")
    elif section_id == "course-conclusion":
        course_title: str = a2_output.get("course_title") or "Untitled Course"
        extracted: dict = shared_state.get("extracted_inputs", {}) or {}
        learning_objectives: list[str] = [str(lo) for lo in (extracted.get("learning_objectives") or [])]
        content_sample: str = extracted.get("content_sample", "") or ""
        sections: list[dict] = a2_output.get("sections") or []
        new_content = _build_course_conclusion(
            course_title,
            content_sample=content_sample,
            learning_objectives=learning_objectives,
            generated_sections=sections,
        )
        if not new_content:
            raise ValueError(f"LLM returned empty content for section '{section_id}'")
    else:
        raise ValueError(f"Cannot regenerate special section '{section_id}'")

    _persist_section_text(job_id, section_id, new_content)
    return new_content


def _set_trace_context_for_job(job_id: str, *, study_guide_path: str | None = None) -> None:
    """Tag LLM traces with run_id + doc_name so costing can attribute per-document spend."""
    from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

    store = get_local_course_job_store()
    job = store.get(job_id)
    doc_name = ""
    if study_guide_path:
        doc_name = Path(study_guide_path).stem
    if not doc_name and job and job.course_title:
        doc_name = sanitize_course_slug(job.course_title)
    if not doc_name:
        doc_name = job_id[:8]
    set_run_context(job_id, doc_name)


def _rewrite_section_sync(job_id: str, section_id: str, current_content: str, user_prompt: str) -> str:
    """Rewrite section content using LLM with user-provided instructions."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    instructions = user_prompt.strip() or "Improve clarity and flow while preserving the original meaning."
    system = (
        "You are an expert course content writer. "
        "Rewrite the provided course section following the user's instructions exactly. "
        "Preserve all factual content and learning points. "
        "Output only the rewritten section content — no preamble, labels, or explanation."
    )
    user_msg = f"CURRENT SECTION CONTENT:\n{current_content}\n\nREWRITE INSTRUCTIONS:\n{instructions}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="A2")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content


def _improve_tone_sync(job_id: str, section_id: str, current_content: str, tone_prompt: str) -> str:
    """Adjust tone and style of section content using LLM."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    tone = tone_prompt.strip() or "Professional, clear, and engaging"
    system = (
        "You are an expert course content editor specialising in tone and style. "
        "Rewrite the provided course section in the requested tone and style. "
        "Preserve all factual content and learning points. "
        "Output only the revised content — no preamble, labels, or explanation."
    )
    user_msg = f"CURRENT SECTION CONTENT:\n{current_content}\n\nDESIRED TONE/STYLE:\n{tone}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="A2")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content


def _summarize_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """Summarize section content to a concise version preserving all key learning points."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    system = (
        "You are an expert course content editor. "
        "Summarize the following course section into a concise version that retains all key "
        "learning points, facts, and concepts. Target roughly 40–60% of the original length. "
        "Use clear, direct language. "
        "Output only the summarized content — no preamble, labels, or explanation."
    )
    user_msg = f"COURSE SECTION CONTENT:\n{current_content}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="editor")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content


def _expand_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """Expand section content with additional depth, examples, and elaboration."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    system = (
        "You are an expert course content writer. "
        "Expand the following course section by adding more depth, concrete examples, "
        "and elaboration on key concepts. Maintain the same educational tone and style. "
        "Only deepen what is already present — do not introduce unrelated topics. "
        "Output only the expanded content — no preamble, labels, or explanation."
    )
    user_msg = f"COURSE SECTION CONTENT:\n{current_content}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="editor")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content


def _simplify_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """Simplify section content using plainer language and shorter sentences."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    system = (
        "You are an expert course content editor. "
        "Simplify the following course section by using plainer language, shorter sentences, "
        "and a more accessible writing style. Avoid jargon where possible; "
        "when technical terms are necessary, briefly explain them. "
        "Preserve all factual content and learning points exactly. "
        "Output only the simplified content — no preamble, labels, or explanation."
    )
    user_msg = f"COURSE SECTION CONTENT:\n{current_content}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="editor")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content



def _regenerate_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """
    Regenerate a single section's content via LLM and persist to shared_state.
    Returns the new plain-text content for the section.
    """
    # Special sections (overview / conclusion) use their own dedicated generators
    if section_id in ("course-overview", "course-conclusion"):
        return _regenerate_special_section_sync(job_id, section_id)
    if section_id == "course-learning-objectives":
        return current_content  # LOs are derived from source; not regeneratable here

    from lectora_backend.pipeline.agent.a2_content_generator.utils.content_writer import (
        generate_lesson,
    )
    from lectora_backend.pipeline.agent.a2_content_generator.utils.source_chunker import (
        extract_full_section_text,
        load_doc_paragraphs,
    )
    from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack

    store = get_local_course_job_store()
    job = store.get(job_id) or _recover_job_from_disk(job_id)
    if not job or not job.shared_state_path:
        raise ValueError(f"Job {job_id} not found or has no shared state")

    with open(job.shared_state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    sections: list[dict] = a2_output.get("sections") or []

    # Find target section
    target_sec = next((s for s in sections if s.get("section_id") == section_id), None)
    if not target_sec:
        raise ValueError(f"Section '{section_id}' not found in A2 output")

    extracted = shared_state.get("extracted_inputs", {}) or {}
    learning_objectives: list[str] = [str(lo) for lo in (extracted.get("learning_objectives") or [])]

    rule_family: str | None = (
        shared_state.get("request_spec", {}).get("rule_classification", {}).get("family")
    )
    difficulty: str = shared_state.get("course_difficulty") or "intermediate"
    rule_pack = resolve_rule_pack(rule_family, difficulty) if rule_family else None
    if not rule_pack:
        raise ValueError(f"Cannot resolve rule pack for family '{rule_family}'")

    # Locate original para_start / para_end from enriched_sections (Section Mapper output)
    enriched_sections: list[dict] = (
        shared_state.get("agent_outputs", {})
        .get("section_map", {})
        .get("enriched_sections", [])
    )
    para_start = para_end = 0
    for lesson in enriched_sections:
        for sub in lesson.get("subtopics") or []:
            if sub.get("id") == section_id:
                para_start = int(sub.get("para_start") or 0)
                para_end = int(sub.get("para_end") or 0)
                break

    # Extract source text from original input doc (DOCX or PDF) when available
    source_text = ""
    if job.input_docx_path and Path(job.input_docx_path).exists() and (para_start or para_end):
        try:
            doc_paragraphs = load_doc_paragraphs(
                job.input_docx_path,
                shared_state_path=job.shared_state_path,
            )
            source_text = extract_full_section_text(
                doc_paragraphs, para_start=para_start, para_end=para_end
            )
        except Exception:
            source_text = ""

    # Fall back to current FE content if no source doc available
    if not source_text:
        source_text = current_content or ""

    word_count = int(target_sec.get("word_count") or 400)
    lesson_title = target_sec.get("outline_lesson") or ""
    lesson_entry = next(
        (l for l in enriched_sections if l.get("title") == lesson_title),
        {"title": lesson_title, "word_count": str(word_count), "minutes": "5"},
    )

    subtopic_spec = {
        "heading": target_sec.get("heading", ""),
        "target_word_count": word_count,
        "source_text": source_text,
        "has_knowledge_check": bool(target_sec.get("has_knowledge_check")),
        "maps_to_objectives": target_sec.get("maps_to_objectives") or [],
        "subtopics": target_sec.get("subtopics") or [],
        "interactive_elements": [],
        "image_count": 0,
        "target_minutes": 0,
    }

    results = generate_lesson(
        lesson=lesson_entry,
        subtopic_specs=[subtopic_spec],
        learning_objectives=learning_objectives,
        prior_summary="",
        rule_pack=rule_pack,
        lesson_wc=word_count,
    )

    if not results:
        raise ValueError("LLM returned no result for section regeneration")

    new_data = results[0]
    new_body = new_data.get("body_paragraphs") or []
    new_wc = int(new_data.get("word_count") or word_count)

    # Persist updated section to shared_state
    for sec in sections:
        if sec.get("section_id") == section_id:
            sec["body_paragraphs"] = new_body
            sec["word_count"] = new_wc
            sec["status"] = "regenerated"
            break

    a2_output["sections"] = sections
    shared_state["agent_outputs"]["A2"] = a2_output
    with open(job.shared_state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

    return _paragraphs_to_text(new_body)


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────────────

class InputDoc(BaseModel):
    blob_path: str = Field(alias="blobPath")

    model_config = {"populate_by_name": True}


class JobInputs(BaseModel):
    study_guide: InputDoc = Field(alias="studyGuide")
    timed_outline: InputDoc | None = Field(default=None, alias="timedOutline")

    model_config = {"populate_by_name": True}


class SourceFileSpecPayload(BaseModel):
    blob_path: str = Field(alias="blobPath")
    extract_hint: str | None = Field(default=None, alias="extractHint")
    importance: str | None = Field(default=None, alias="importance")

    model_config = {"populate_by_name": True}


class CourseConfigPayload(BaseModel):
    """Onboarding wizard fields forwarded to A2 for dynamic prompt construction."""
    # User-provided title and description — always the single source of truth.
    course_title: str | None = Field(default=None, alias="courseTitle")
    course_description: str | None = Field(default=None, alias="courseDescription")
    experience_level: str | None = Field(default=None, alias="experienceLevel")
    learner_outcomes: str | None = Field(default=None, alias="learnerOutcomes")
    audience_notes: str | None = Field(default=None, alias="audienceNotes")
    learning_objectives: list[str] = Field(default_factory=list, alias="learningObjectives")
    tone: str | None = Field(default=None)
    depth: str | None = Field(default=None)
    emphasis: str | None = Field(default=None)
    avoid: str | None = Field(default=None)
    include_scenarios: bool | None = Field(default=None, alias="includeScenarios")
    include_knowledge_checks: bool | None = Field(default=None, alias="includeKnowledgeChecks")

    model_config = {"populate_by_name": True}


class CreateJobPayload(BaseModel):
    course_title: str = Field(alias="courseTitle")
    course_type: str = Field(alias="courseType")
    difficulty: str = Field(default="intermediate")
    inputs: JobInputs
    to_override: dict[str, Any] | None = Field(default=None, alias="toOverride")
    # Per-file source specs (blob path + optional extract hint + importance).
    source_file_specs: list[SourceFileSpecPayload] | None = Field(default=None, alias="sourceFileSpecs")
    # Target audience — drives prompt calibration in A2 content generation.
    audience: str = Field(default="", alias="audience")
    # Optional special instructions provided by the user before course generation.
    # Injected into A2 prompts to influence tone, depth, and emphasis.
    special_instructions: str | None = Field(default=None, alias="specialInstructions")
    # All wizard onboarding fields for dynamic A2 prompt construction.
    course_config: CourseConfigPayload | None = Field(default=None, alias="courseConfig")

    model_config = {"populate_by_name": True}


# ─────────────────────────────────────────────────────────────────────────────
# TO-override helpers (mirrors pipeline_adapter._llm_outline_from_to_data)
# ─────────────────────────────────────────────────────────────────────────────

def _llm_outline_from_to_data(to_data: dict[str, Any]) -> dict[str, Any]:
    sections = []
    for s in to_data.get("sections") or []:
        raw_subtopics = s.get("subtopics") or []
        sections.append({
            "title": s.get("title") or "",
            "word_count": s.get("word_count"),
            "minutes": s.get("duration_minutes"),
            "credit_hours": s.get("credit_hours"),
            "content": s.get("content_summary") or "",
            "interactive_elements": s.get("interactive_elements") or [],
            "subtopics": [
                {"title": t} if isinstance(t, str) else t
                for t in raw_subtopics
            ],
        })
    return {
        "course_title": to_data.get("course_name") or to_data.get("course_title") or "",
        "description": to_data.get("description") or "",
        "learning_objectives": to_data.get("learning_objectives") or [],
        "totals": {
            "word_count": to_data.get("total_word_count"),
            "minutes": to_data.get("total_minutes"),
            "credit_hours": to_data.get("total_credit_hours"),
        },
        "sections": sections,
        "_user_edited": True,
        "_reused_from_preview": True,
    }


def _write_to_override(to_data: dict[str, Any], temp_dir: Path) -> Path:
    """Write user-edited TO as a JSON file A0 loads directly (no LLM call)."""
    path = temp_dir / "user_edited_to.json"
    payload = {"llm_to_outline": _llm_outline_from_to_data(to_data)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _persist_difficulty(shared_state_path: str, difficulty: str) -> None:
    """Inject course_difficulty into shared_state.json for downstream agents."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["course_difficulty"] = difficulty.strip().lower()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _persist_source_file_paths(shared_state_path: str, paths: list[str]) -> None:
    """Store source file local paths in shared_state for A2 chunk-based retrieval."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["source_file_paths"] = paths
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _persist_source_file_specs(shared_state_path: str, specs: list[dict]) -> None:
    """Store per-file source specs (extract_hint, importance) in shared_state for A2."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["source_file_specs"] = specs
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _persist_audience(shared_state_path: str, audience: str) -> None:
    """Store target audience in shared_state for A2 prompt calibration."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["course_audience"] = audience.strip()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _persist_special_instructions(shared_state_path: str, instructions: str) -> None:
    """Store user special instructions in shared_state for A2 prompt injection."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["special_instructions"] = instructions.strip()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _persist_course_title(shared_state_path: str, title: str) -> None:
    """Store user-provided course title in shared_state so A2 uses it verbatim."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["course_title"] = title.strip()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _persist_course_config(shared_state_path: str, course_config: "CourseConfigPayload") -> None:
    """Store wizard onboarding fields in shared_state for A2 dynamic prompt construction.

    Only prompt-guidance fields (tone, depth, emphasis, etc.) are written here.
    course_title, course_description, and learning_objectives are intentionally
    NOT overwritten — A2 reads them directly from llm_to_outline_classification,
    which holds the exact TO the LLM generated (or the user's edited TO).
    """
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    config_dict: dict[str, Any] = {}
    # Prompt-guidance fields only — do NOT include course_title or course_description.
    if course_config.experience_level:
        config_dict["experience_level"] = course_config.experience_level
    if course_config.learner_outcomes and course_config.learner_outcomes.strip():
        config_dict["learner_outcomes"] = course_config.learner_outcomes.strip()
    if course_config.audience_notes and course_config.audience_notes.strip():
        config_dict["audience_notes"] = course_config.audience_notes.strip()
    if course_config.learning_objectives:
        config_dict["learning_objectives"] = list(course_config.learning_objectives)
    if course_config.tone and course_config.tone.strip():
        config_dict["tone"] = course_config.tone.strip()
    if course_config.depth and course_config.depth.strip():
        config_dict["depth"] = course_config.depth.strip()
    if course_config.emphasis and course_config.emphasis.strip():
        config_dict["emphasis"] = course_config.emphasis.strip()
    if course_config.avoid and course_config.avoid.strip():
        config_dict["avoid"] = course_config.avoid.strip()
    if course_config.include_scenarios is not None:
        config_dict["include_scenarios"] = course_config.include_scenarios
    if course_config.include_knowledge_checks is not None:
        config_dict["include_knowledge_checks"] = course_config.include_knowledge_checks
    state["course_config"] = config_dict
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _sync_legacy_shared_state_dir(course_slug: str) -> None:
    """Mirror ``pipeline/courses/{slug}`` into legacy ``pipeline/shared_state/{slug}``."""
    if not course_slug:
        return
    source_dir = (_PIPELINE_COURSES_DIR / course_slug).resolve()
    if not source_dir.is_dir():
        return
    target_dir = (_LEGACY_SHARED_STATE_DIR / course_slug).resolve()
    shutil.rmtree(target_dir, ignore_errors=True)
    shutil.copytree(source_dir, target_dir)


def _format_s1_feedback(report: Any) -> str:
    lines: list[str] = []
    for issue in getattr(report, "issues", []) or []:
        sev = getattr(issue, "severity", "")
        if sev == "blocker":
            lines.append(f"[BLOCKER] {issue.field}: {issue.message} (rule: {issue.rule_source})")
        elif sev == "warning":
            lines.append(f"[WARNING] {issue.field}: {issue.message} (rule: {issue.rule_source})")
    return "\n".join(lines)


def _format_s2_feedback(report: Any) -> str:
    lines: list[str] = []
    for issue in getattr(report, "issues", []) or []:
        sev = getattr(issue, "severity", "")
        if sev in ("blocker", "critical"):
            lines.append(f"  - [{issue.field}] {issue.message} (rule: {issue.rule_source})")
    return "\n".join(lines)


def _s1_blocks(status: Any) -> bool:
    return status in (S1Status.blocked, S1Status.blocker)


def _s2_blocks(status: Any) -> bool:
    return status in (S2Status.blocked, S2Status.blocker)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline runner (blocking — runs inside asyncio.to_thread)
# ─────────────────────────────────────────────────────────────────────────────

def _run_pipeline_sync(
    job_id: str,
    study_guide_path: str,
    timed_outline_path: str | None,
    to_override: dict[str, Any] | None,
    difficulty: str,
    source_file_paths: list[str] | None = None,
    audience: str = "",
    special_instructions: str | None = None,
    source_file_specs: list[dict] | None = None,
    course_config: "CourseConfigPayload | None" = None,
) -> tuple[str | None, str | None]:
    """Execute the full pipeline synchronously.  Returns (shared_state_path, study_guide_docx_path)."""
    store = get_local_course_job_store()
    # Temp dir is used only for ephemeral input files (e.g. user_edited_to.json).
    # All pipeline output artifacts go to the persistent shared_state directory
    # so they land in the same location as direct pipeline.py runs.
    temp_input_dir = Path(tempfile.mkdtemp(prefix=f"lectora_job_{job_id[:8]}_"))

    try:
        return _run_pipeline_inner(
            job_id, study_guide_path, timed_outline_path, to_override, difficulty,
            temp_input_dir, source_file_paths, audience, special_instructions,
            source_file_specs=source_file_specs,
            course_config=course_config,
        )
    finally:
        # Ephemeral input dir is tiny (user_edited_to.json only); clean up always.
        shutil.rmtree(temp_input_dir, ignore_errors=True)


def _run_pipeline_inner(
    job_id: str,
    study_guide_path: str,
    timed_outline_path: str | None,
    to_override: dict[str, Any] | None,
    difficulty: str,
    temp_input_dir: Path,
    source_file_paths: list[str] | None = None,
    audience: str = "",
    special_instructions: str | None = None,
    source_file_specs: list[dict] | None = None,
    course_config: "CourseConfigPayload | None" = None,
) -> tuple[str | None, str | None]:
    """Inner pipeline body, called from _run_pipeline_sync after temp dir setup."""
    store = get_local_course_job_store()

    def log(level: str, message: str, stage: str | None = None) -> None:
        store.append_log(job_id, level, message, stage)
        logger.info("[%s] [%s] %s", job_id[:8], stage or "pipeline", message)

    _set_trace_context_for_job(job_id, study_guide_path=study_guide_path)

    # Resolve effective TO path for first gate cycle
    effective_to_path: str | None = None
    if to_override and isinstance(to_override, dict):
        override_path = _write_to_override(to_override, temp_input_dir)
        effective_to_path = str(override_path)
        log("info", "Using your reviewed Training Outline — skipping outline regeneration")
    elif timed_outline_path:
        effective_to_path = timed_outline_path

    # ── A0 → A1 → S1 gate loop ───────────────────────────────────────────
    shared_state_path: str | None = None
    s1_feedback: str | None = None

    # Compute per-job output slug once — each run gets its own isolated dir:
    # pipeline/courses/{course_slug}/{job_id}/
    _job_rec = store.get(job_id)
    course_slug = sanitize_course_slug(_job_rec.course_title if _job_rec else "course")
    job_output_slug = f"{course_slug}/{job_id}"
    artifact_dir = _PIPELINE_COURSES_DIR / course_slug / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "logs").mkdir(parents=True, exist_ok=True)
    store.set_artifact_dir(job_id, str(artifact_dir))

    for gate_cycle in range(1, MAX_A0_A1_S1_CYCLES + 1):
        if store.is_cancelled(job_id):
            raise RuntimeError("Cancelled")
        if gate_cycle == 1:
            log("info", "Analyzing your study guide document…", "A0")
        else:
            log("info", f"Re-analyzing document with quality feedback (attempt {gate_cycle})…", "A0")
        store.start_stage(job_id, "A0")

        # Always keep the user-supplied TO across retry cycles.
        # effective_to_path holds either the three-panel JSON override or the
        # original uploaded TO file. Falling back to timed_outline_path on
        # retries is wrong when to_override was used (timed_outline_path may be
        # None) — that caused A0 to silently discard the user's TO and generate
        # a fresh one, losing learning objectives and section structure.
        to_path_for_cycle = effective_to_path

        # Route the study guide to the correct parser based on file extension.
        # A0 accepts docx_paths (python-docx) or pdf_paths (pypdf); passing a
        # PDF as a DOCX path causes python-docx to crash.
        _sg_ext = Path(study_guide_path).suffix.lower()
        _a0_docx_paths: list[str] = [] if _sg_ext == ".pdf" else [study_guide_path]
        _a0_pdf_paths: list[str] = [study_guide_path] if _sg_ext == ".pdf" else []

        a0_result = A0RequestSynthesizer(
            docx_paths=_a0_docx_paths,
            pdf_paths=_a0_pdf_paths,
            to_outline_doc_path=to_path_for_cycle,
            output_dir=str(_PIPELINE_COURSES_DIR),
            course_difficulty=difficulty,
            course_output_slug=job_output_slug,
        ).run()
        shared_state_path = a0_result.shared_state_path
        # Point the job's artifact dir to the actual pipeline output directory
        # (parent of shared_state.json = pipeline/courses/{course_slug}/{job_id}/).
        store.set_temp_dir(job_id, str(Path(shared_state_path).parent))
        _persist_difficulty(shared_state_path, difficulty)

        # Inject source_file_paths into shared_state so A2 can build a chunk
        # index for topic-wise retrieval across all uploaded source files.
        if source_file_paths:
            _persist_source_file_paths(shared_state_path, source_file_paths)
        # Persist per-file specs (extract_hint, importance) for A2 prompt guidance.
        if source_file_specs:
            _persist_source_file_specs(shared_state_path, source_file_specs)
        # Persist audience so A2 can calibrate content for the correct learner profile.
        if audience:
            _persist_audience(shared_state_path, audience)
        # Persist special instructions so A2 can inject them into generation prompts.
        if special_instructions:
            _persist_special_instructions(shared_state_path, special_instructions)
        # Persist wizard onboarding config for dynamic A2 prompt construction.
        if course_config:
            _persist_course_config(shared_state_path, course_config)
        # Persist user-provided course title so A2 never substitutes the LLM-extracted one.
        _job_title = (store.get(job_id) or _job_rec)
        if _job_title and _job_title.course_title:
            _persist_course_title(shared_state_path, _job_title.course_title)
        _sync_legacy_shared_state_dir(job_output_slug)
        log("success", "Document analyzed — course structure and rule family identified", "A0")
        store.complete_stage(job_id, "A0", "PASS")

        log("info", "Extracting knowledge and building enriched course outline…", "A1")
        store.start_stage(job_id, "A1")

        a1_feedback = (
            {"validator_feedback": s1_feedback, "gateAttempt": gate_cycle}
            if s1_feedback
            else None
        )
        a1_run(
            shared_state_path=shared_state_path,
            docx_path=study_guide_path,
            feedback=a1_feedback,
        )
        _sync_legacy_shared_state_dir(job_output_slug)
        log("success", "Course outline built — sections and learning objectives mapped", "A1")
        store.complete_stage(job_id, "A1", "PASS")

        log("info", "Reviewing course structure for quality and compliance…", "S1")
        store.start_stage(job_id, "S1")
        s1_result = S1Validator(shared_state_path).run()
        _sync_legacy_shared_state_dir(job_output_slug)

        if not _s1_blocks(s1_result.status):
            log("success", "Structure review passed — course outline meets quality standards", "S1")
            store.complete_stage(job_id, "S1", "PASS")
            break

        # S1 blocked — surface issues and prepare feedback for next cycle
        blocker_issues = [
            {"message": getattr(i, "message", str(i)), "field": getattr(i, "field", "")}
            for i in (getattr(s1_result, "issues", []) or [])
            if getattr(i, "severity", "") == "blocker"
        ]
        store.complete_stage(job_id, "S1", "BLOCKED", blockers=blocker_issues, retry_attempt=gate_cycle)

        if gate_cycle < MAX_A0_A1_S1_CYCLES:
            s1_feedback = _format_s1_feedback(s1_result)
            log("warn", "Structure review found issues — improving outline and retrying…", "S1")
        else:
            log("error", "Structure review could not be resolved — pipeline stopped", "S1")
            raise RuntimeError("S1 validation blocked after max retries")

    if store.is_cancelled(job_id):
        raise RuntimeError("Cancelled")

    # ── Section Mapper ────────────────────────────────────────────────────
    assert shared_state_path is not None
    log("info", "Organizing course sections and mapping lessons to the training outline…", "SECTION_MAPPER")
    store.start_stage(job_id, "SECTION_MAPPER")
    section_mapper_run(shared_state_path=shared_state_path)
    _sync_legacy_shared_state_dir(job_output_slug)
    log("success", "Course sections organized and lesson mapping complete", "SECTION_MAPPER")
    store.complete_stage(job_id, "SECTION_MAPPER", "PASS")

    # ── KC Planner ────────────────────────────────────────────────────────
    log("info", "Planning interactive knowledge check placement…", "KC_PLANNER")
    store.start_stage(job_id, "KC_PLANNER")
    kc_result = kc_planner_run(shared_state_path=shared_state_path)
    _sync_legacy_shared_state_dir(job_output_slug)
    kc_count = kc_result.get("kc_count", 0)
    log("success", f"Knowledge check placement complete — {kc_count} interactive checks planned", "KC_PLANNER")
    store.complete_stage(job_id, "KC_PLANNER", "PASS")

    # ── A2 → S2 loop ──────────────────────────────────────────────────────
    log("info", "Generating comprehensive content for each lesson…", "A2")
    store.start_stage(job_id, "A2")

    a2_feedback: str | None = None
    s2_result: Any = None
    final_docx_path: str | None = None

    for a2_cycle in range(1, MAX_A2_S2_CYCLES + 1):
        if store.is_cancelled(job_id):
            raise RuntimeError("Cancelled")
        if a2_cycle > 1:
            log("info", "Refining course content based on quality feedback…", "A2")

        a2_result = A2ContentGenerator(
            shared_state_path=shared_state_path,
            docx_path=study_guide_path,
            render_docx=False,
            course_difficulty=difficulty,
            feedback=a2_feedback,
            source_file_paths=source_file_paths,
        ).run()
        _sync_legacy_shared_state_dir(job_output_slug)
        log(
            "success",
            f"Content generation complete — {a2_result.stats.generated} lessons written "
            f"({a2_result.stats.total_words:,} words)",
            "A2",
        )

        log("info", "Checking content quality, accuracy, and completeness…", "S2")
        store.start_stage(job_id, "S2")
        s2_result = S2Validator(shared_state_path).run()
        _sync_legacy_shared_state_dir(job_output_slug)

        if not _s2_blocks(s2_result.status):
            log("success", "Content quality review passed", "S2")
            store.complete_stage(job_id, "A2", "PASS")
            store.complete_stage(job_id, "S2", "PASS")
            break

        blocker_issues = [
            {"message": getattr(i, "message", str(i)), "field": getattr(i, "field", "")}
            for i in (getattr(s2_result, "issues", []) or [])
            if getattr(i, "severity", "") == "blocker"
        ]
        store.complete_stage(job_id, "S2", "BLOCKED", blockers=blocker_issues, retry_attempt=a2_cycle)

        if a2_cycle < MAX_A2_S2_CYCLES:
            a2_feedback = _format_s2_feedback(s2_result)
            log("warn", "Content quality issues found — regenerating with improvements…", "S2")
        else:
            log("error", "Content quality could not be resolved after multiple attempts", "S2")
            store.complete_stage(job_id, "A2", "FAILED")

    # Render study_guide.docx only when S2 cleared
    if s2_result and not _s2_blocks(s2_result.status):
        log("info", "Assembling your final course document…", "A2")
        final_docx_path = render_study_guide_from_state(shared_state_path=shared_state_path)
        _sync_legacy_shared_state_dir(job_output_slug)
        log("success", "Your course document is ready", "A2")
    else:
        _sync_legacy_shared_state_dir(job_output_slug)
        log("error", "Course document could not be assembled — quality gate blocked", "A2")

    return shared_state_path, final_docx_path


# ─────────────────────────────────────────────────────────────────────────────
# Background task wrapper
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline_background(
    job_id: str,
    study_guide_path: str,
    timed_outline_path: str | None,
    to_override: dict[str, Any] | None,
    difficulty: str,
    source_file_paths: list[str] | None = None,
    audience: str = "",
    special_instructions: str | None = None,
    source_file_specs: list[dict] | None = None,
    course_config: "CourseConfigPayload | None" = None,
) -> None:
    store = get_local_course_job_store()
    store.update_status(job_id, LocalJobStatus.PROCESSING)
    store.set_input_docx(job_id, study_guide_path)
    store.append_log(job_id, "info", "Course generation started", None)

    def _sync() -> tuple[str | None, str | None]:
        return _run_pipeline_sync(
            job_id, study_guide_path, timed_outline_path, to_override, difficulty,
            source_file_paths, audience, special_instructions,
            source_file_specs=source_file_specs,
            course_config=course_config,
        )

    try:
        shared_state_path, study_guide_docx = await asyncio.to_thread(_sync)
        store.complete_job(
            job_id,
            shared_state_path=shared_state_path,
            study_guide_path=study_guide_docx,
        )

        # Sync all pipeline JSON/DOCX artifacts to Azure Blob Storage.
        if shared_state_path:
            from lectora_backend.core.local_artifact_sync import sync_local_artifacts_to_azure

            job = store.get(job_id)
            course_title = job.course_title if job else "course"
            sync_result = await asyncio.to_thread(
                sync_local_artifacts_to_azure,
                job_id=job_id,
                course_title=course_title,
                shared_state_path=shared_state_path,
                study_guide_path=study_guide_docx,
            )
            if sync_result.get("skipped"):
                logger.debug(
                    "[%s] Azure artifact sync skipped: %s",
                    job_id[:8],
                    sync_result.get("reason"),
                )
            elif sync_result.get("uploaded", 0) > 0:
                blob_root = sync_result.get("blobRoot")
                if blob_root:
                    store.set_azure_blob_root(job_id, blob_root)
                store.append_log(
                    job_id,
                    "success",
                    f"Uploaded {sync_result['uploaded']} artifact(s) to Azure "
                    f"({sync_result.get('container')}/{blob_root})",
                    None,
                )
            if sync_result.get("errors"):
                store.append_log(
                    job_id,
                    "warn",
                    f"Some artifacts failed to upload to Azure ({len(sync_result['errors'])} error(s))",
                    None,
                )

        store.append_log(job_id, "success", "Your course has been generated successfully", None)
    except Exception as exc:
        if str(exc) == "Cancelled" or store.is_cancelled(job_id):
            store.cancel_job(job_id, reason="Cancelled")
            store.append_log(job_id, "warn", "Course generation cancelled", None)
            logger.info("[%s] Pipeline cancelled", job_id[:8])
        else:
            logger.exception("[%s] Pipeline failed: %s", job_id[:8], exc)
            store.fail_job(
                job_id,
                error={"message": str(exc), "type": type(exc).__name__},
            )
            store.append_log(job_id, "error", f"Generation failed: {exc}", None)
    finally:
        unregister_local_pipeline(job_id)
        store.release_slot()


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create and start a local pipeline job",
)
async def create_job(payload: CreateJobPayload) -> JSONResponse:
    store = get_local_course_job_store()

    if not store.acquire_slot():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Server busy — max concurrent pipeline jobs reached "
                f"(active: {store.active_count()}). Retry in a moment."
            ),
        )

    # ── Resolve study guide (required) ───────────────────────────────────────
    try:
        study_guide_blob = _resolve_and_validate(
            payload.inputs.study_guide.blob_path, "Study guide"
        )
    except HTTPException:
        store.release_slot()
        raise

    # ── Resolve timed outline (optional) ─────────────────────────────────────
    # When to_override is provided the pipeline injects the TO JSON directly and
    # never reads the timed outline file — so a missing file is not an error.
    timed_outline_path: str | None = None
    if payload.inputs.timed_outline:
        to_raw = payload.inputs.timed_outline.blob_path
        if payload.to_override:
            # to_override takes precedence; try to resolve the file but don't fail
            try:
                timed_outline_path = _resolve_and_validate(to_raw, "Timed outline")
            except HTTPException:
                logger.info(
                    "[create_job] Timed outline %r not found — to_override present, skipping file",
                    to_raw,
                )
                timed_outline_path = None
        else:
            # No to_override — the pipeline MUST read the timed outline file
            try:
                timed_outline_path = _resolve_and_validate(to_raw, "Timed outline")
            except HTTPException:
                store.release_slot()
                raise

    difficulty = (payload.difficulty or "intermediate").strip().lower()
    job = store.create(
        course_title=payload.course_title,
        course_type=payload.course_type,
        difficulty=difficulty,
    )

    # ── Resolve source file specs (best-effort, non-fatal) ───────────────────
    # Missing source files only affect multi-file chunk retrieval in A2.
    # Silently drop specs whose paths cannot be resolved so a single stale path
    # does not block the whole job.
    source_file_paths: list[str] | None = None
    source_file_specs: list[dict] | None = None
    raw_blob_paths: list[str] = []
    if payload.source_file_specs:
        from lectora_backend.core.blob_resolver import resolve_blob_to_local
        resolved_paths: list[str] = []
        resolved_specs: list[dict] = []
        for spec in payload.source_file_specs:
            r = resolve_blob_to_local(spec.blob_path)
            if r is not None:
                local_path = str(r)
                resolved_paths.append(local_path)
                resolved_specs.append({
                    "blob_path": spec.blob_path,
                    "local_path": local_path,
                    "extract_hint": spec.extract_hint or "",
                    "importance": spec.importance or "medium",
                })
                raw_blob_paths.append(spec.blob_path)
            else:
                logger.warning("[create_job] Source file not found (skipped): %r", spec.blob_path)
        if resolved_paths:
            source_file_paths = resolved_paths
            source_file_specs = resolved_specs

    course_slug = sanitize_course_slug(payload.course_title)
    register_local_pipeline(
        job.job_id,
        course_title=payload.course_title,
        course_slug=course_slug,
        blob_paths=raw_blob_paths or [payload.inputs.study_guide.blob_path],
    )

    asyncio.create_task(
        _run_pipeline_background(
            job_id=job.job_id,
            study_guide_path=study_guide_blob,
            timed_outline_path=timed_outline_path,
            to_override=payload.to_override,
            difficulty=difficulty,
            source_file_paths=source_file_paths,
            audience=payload.audience,
            special_instructions=payload.special_instructions,
            source_file_specs=source_file_specs,
            course_config=payload.course_config,
        )
    )

    logger.info(
        "[create_job] Started job %s | title=%r | difficulty=%s",
        job.job_id,
        payload.course_title,
        difficulty,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"jobId": job.job_id, "status": "PENDING"},
    )


# ─── Filesystem-based job reconstruction helpers ──────────────────────────────

def _job_id_from_state_file(state_file: Path) -> str:
    """Derive job_id from the directory layout when shared_state lacks run.jobId."""
    parent = state_file.parent
    if parent.name == "state":
        return parent.parent.name
    return parent.name


def _artifact_dir_from_state_file(state_file: Path) -> Path:
    """Return the per-job artifact root (parent of state/ or of shared_state.json)."""
    parent = state_file.parent
    if parent.name == "state":
        return parent.parent
    return parent


def _resolve_study_guide_path(artifact_dir: Path) -> Path | None:
    for candidate in (
        artifact_dir / "study_guide.docx",
        artifact_dir / "output" / "study_guide.docx",
    ):
        if candidate.is_file():
            return candidate
    return None


def _course_title_from_shared_state(state: dict, course_slug: str) -> str:
    # The TO (llm_to_outline_classification) is the single source of truth for
    # course_title. It holds the exact LLM-generated title (or the user's edited
    # TO title when to_override was used). All downstream stages use this same value.
    # course_metadata.title can be a section heading (e.g. "3.0 What long-term care…")
    # when A1 incorrectly lifts it from the document's first section, so it goes last.
    to_outline = state.get("llm_to_outline_classification") or {}
    to_course_title = (to_outline.get("course_title") or to_outline.get("course_name") or "").strip()
    a2_course_title = ((state.get("agent_outputs") or {}).get("A2") or {}).get("course_title") or ""
    request_spec = state.get("request_spec") or {}
    course_metadata = request_spec.get("course_metadata") or {}
    return (
        to_course_title                                        # TO title — single source of truth
        or a2_course_title                                     # A2 output (fallback when TO not yet run)
        or state.get("course_title")                          # legacy field
        or (state.get("request") or {}).get("courseTitle")   # job request body fallback
        or course_slug.replace("_", " ")                      # slug fallback (never a section heading)
        or course_metadata.get("title")                       # last resort (may be a section heading)
    )


def _course_type_from_shared_state(state: dict) -> str:
    request_spec = state.get("request_spec") or {}
    course_metadata = request_spec.get("course_metadata") or {}
    return (
        state.get("request", {}).get("courseType")
        or course_metadata.get("course_type")
        or "insurance_ce"
    )


def _collect_state_files(course_dir: Path) -> list[Path]:
    """All shared_state.json locations under one course folder, newest first."""
    patterns = (
        "*/shared_state.json",
        "*/state/shared_state.json",
        "*/state/pipeline_shared_state.json",
        "state/shared_state.json",
    )
    found: dict[str, Path] = {}
    for pattern in patterns:
        for path in course_dir.glob(pattern):
            found[str(path.resolve())] = path
    return sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def _reconstruct_job_from_filesystem(
    store: "LocalCourseJobStore",
    course_slug: str,
) -> "LocalCourseJob | None":
    """Scan pipeline/courses/{course_slug}/ for the most recent shared_state.json
    and register a COMPLETED job in the in-memory store.

    This allows the Asset Library to open courses generated before the current
    server session (e.g. after a dev-server restart where the in-memory store
    was cleared but disk artifacts remain).
    """
    from lectora_backend.api.local_course_job_store import LocalCourseJobStore as _Store  # noqa: F401

    course_dir = _PIPELINE_COURSES_DIR / course_slug
    if not course_dir.is_dir():
        return None

    state_files = _collect_state_files(course_dir)

    for state_file in state_files:
        try:
            with open(state_file, encoding="utf-8") as fh:
                state = json.load(fh)

            job_id: str = state.get("run", {}).get("jobId") or _job_id_from_state_file(state_file)
            if not job_id:
                continue

            artifact_dir = _artifact_dir_from_state_file(state_file)
            docx_candidate = _resolve_study_guide_path(artifact_dir)

            return store.register_from_filesystem(
                job_id=job_id,
                course_title=_course_title_from_shared_state(state, course_slug),
                course_type=_course_type_from_shared_state(state),
                shared_state_path=str(state_file),
                study_guide_path=str(docx_candidate) if docx_candidate else None,
                temp_dir=str(artifact_dir),
            )
        except Exception:
            continue

    return None


def _find_shared_state_for_job_id(job_id: str) -> Path | None:
    """Return the path to shared_state.json for the given job_id, or None.

    Supports both the new isolated layout ({course_slug}/{job_id}/state/) and
    the legacy layout ({course_slug}/state/).  Scans pipeline/courses/.
    """
    for pattern in (
        f"*/{job_id}/shared_state.json",
        f"*/{job_id}/state/shared_state.json",
        f"*/{job_id}/state/pipeline_shared_state.json",
    ):
        matches = sorted(
            _PIPELINE_COURSES_DIR.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]

    # Legacy layout: the shared_state.json may embed the jobId in its "run" block
    for state_file in _PIPELINE_COURSES_DIR.glob("*/state/shared_state.json"):
        try:
            with open(state_file, encoding="utf-8") as fh:
                state = json.load(fh)
            if state.get("run", {}).get("jobId") == job_id:
                return state_file
        except Exception:
            continue

    return None


def _load_shared_state_dict(
    job: "LocalCourseJob",
    *,
    course_slug: str | None = None,
) -> dict | None:
    """Load shared state — local disk first, then Azure (with cached blob root)."""
    from lectora_backend.core.azure_course_artifacts import (
        is_azure_artifacts_enabled,
        load_shared_state_for_job,
    )

    if job.shared_state_path and Path(job.shared_state_path).exists():
        try:
            with open(job.shared_state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass

    if is_azure_artifacts_enabled():
        azure_state = load_shared_state_for_job(
            job.job_id,
            blob_root=job.azure_blob_root,
            course_slug=course_slug,
        )
        if azure_state:
            return azure_state

    return None


def _materialize_shared_state_to_disk(
    job: "LocalCourseJob",
    *,
    course_slug: str | None = None,
) -> Path:
    """Return a local shared_state.json path, materializing from Azure when needed."""
    if job.shared_state_path:
        existing = Path(job.shared_state_path)
        if existing.is_file():
            return existing

    shared_state = _load_shared_state_dict(job, course_slug=course_slug)
    if not shared_state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shared state not found — cannot build DOCX",
        )

    if job.temp_dir and Path(job.temp_dir).is_dir():
        work_dir = Path(job.temp_dir)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix=f"lectora_{job.job_id}_"))
        get_local_course_job_store().set_temp_dir(job.job_id, str(work_dir))

    state_path = work_dir / "shared_state.json"
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

    store = get_local_course_job_store()
    store.set_shared_state_path(job.job_id, str(state_path))
    job.shared_state_path = str(state_path)
    return state_path


def _apply_course_title_to_shared_state(state_path: Path, title: str) -> None:
    trimmed = title.strip()
    if not trimmed:
        return
    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)
    a2_output: dict = shared_state.setdefault("agent_outputs", {}).setdefault("A2", {})
    a2_output["course_title"] = trimmed
    shared_state.setdefault("request", {})["courseTitle"] = trimmed
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)


def _load_llm_outline_for_job(job: "LocalCourseJob") -> dict | None:
    from lectora_backend.core.azure_course_artifacts import (
        is_azure_artifacts_enabled,
        load_llm_outline_for_job,
    )

    if is_azure_artifacts_enabled():
        outline = load_llm_outline_for_job(
            job.job_id,
            blob_root=job.azure_blob_root,
        )
        if outline:
            return outline

    if job.temp_dir:
        outline = _load_llm_outline_from_dir(Path(job.temp_dir))
        if outline:
            return outline
    return None


def _load_llm_outline_from_dir(artifact_dir: Path) -> dict | None:
    """Read llm_to_outline from local job folder (flat or Azure-mirrored layout)."""
    for candidate in (
        artifact_dir / "llm_to_outline.json",
        artifact_dir / "output" / "llm_to_outline.json",
    ):
        if not candidate.is_file():
            continue
        try:
            with open(candidate, encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                continue
            inner = payload.get("llm_to_outline")
            return inner if isinstance(inner, dict) else payload
        except Exception:
            continue
    return None


def _resolve_job_from_azure(
    store: "LocalCourseJobStore",
    job_id: str,
    *,
    course_slug: str | None = None,
) -> "LocalCourseJob | None":
    """Reconstruct a COMPLETED job from Azure course-generation-artifacts."""
    from lectora_backend.core.azure_course_artifacts import (
        get_job_artifact_root,
        is_azure_artifacts_enabled,
        load_shared_state_for_job,
    )

    if not is_azure_artifacts_enabled():
        return None

    blob_root = get_job_artifact_root(job_id, course_slug=course_slug)
    raw_state = load_shared_state_for_job(
        job_id,
        blob_root=blob_root,
        course_slug=course_slug,
    )
    if not raw_state:
        return None

    return store.register_from_filesystem(
        job_id=job_id,
        course_title=_course_title_from_shared_state(raw_state, ""),
        course_type=_course_type_from_shared_state(raw_state),
        shared_state_path=None,
        azure_blob_root=blob_root,
    )


def _resolve_job_with_filesystem(
    store: "LocalCourseJobStore",
    job_id: str,
    *,
    course_slug: str | None = None,
) -> "LocalCourseJob | None":
    """Return in-memory job or reconstruct from Azure, then local disk."""
    job = store.get(job_id)
    if job:
        return job

    job = _resolve_job_from_azure(store, job_id, course_slug=course_slug)
    if job:
        return job

    state_file = _find_shared_state_for_job_id(job_id)
    if state_file is None:
        return None
    try:
        with open(state_file, encoding="utf-8") as fh:
            raw_state = json.load(fh)
        artifact_dir = _artifact_dir_from_state_file(state_file)
        docx_candidate = _resolve_study_guide_path(artifact_dir)
        return store.register_from_filesystem(
            job_id=job_id,
            course_title=_course_title_from_shared_state(raw_state, ""),
            course_type=_course_type_from_shared_state(raw_state),
            shared_state_path=str(state_file),
            study_guide_path=str(docx_candidate) if docx_candidate else None,
            temp_dir=str(artifact_dir),
        )
    except Exception:
        return None


@router.get(
    "/by-course-slug/{course_slug}",
    summary="Find the most recent job by course slug",
)
async def get_job_by_course_slug(course_slug: str) -> JSONResponse:
    """Return the most recent job whose course slug matches.

    Checks the in-memory store first; if empty (e.g. after a server restart),
    reconstructs the job from on-disk artifacts so the Asset Library can open
    courses generated in previous sessions.
    """
    from lectora_backend.core.course_storage import sanitize_course_slug

    store = get_local_course_job_store()
    all_jobs = store.list_all()
    matched = [
        j for j in all_jobs
        if sanitize_course_slug(j.course_title) == course_slug
    ]
    if matched:
        best = max(matched, key=lambda j: j.updated_at or j.created_at)
        return JSONResponse(content={
            "jobId": best.job_id,
            "status": best.status.value,
            "courseTitle": best.course_title,
        })

    # Fallback: Azure course-generation-artifacts, then local disk
    from lectora_backend.core.azure_course_artifacts import (
        find_job_id_for_course_slug,
        is_azure_artifacts_enabled,
    )

    if is_azure_artifacts_enabled():
        azure_job_id = find_job_id_for_course_slug(course_slug)
        if azure_job_id:
            reconstructed = _resolve_job_from_azure(store, azure_job_id)
            if reconstructed is not None:
                return JSONResponse(content={
                    "jobId": reconstructed.job_id,
                    "status": reconstructed.status.value,
                    "courseTitle": reconstructed.course_title,
                })

    reconstructed = _reconstruct_job_from_filesystem(store, course_slug)
    if reconstructed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job found for course slug '{course_slug}'.",
        )
    return JSONResponse(content={
        "jobId": reconstructed.job_id,
        "status": reconstructed.status.value,
        "courseTitle": reconstructed.course_title,
    })


@router.delete(
    "/{job_id}",
    summary="Cancel and remove a local pipeline job",
)
async def delete_job(job_id: str) -> JSONResponse:
    """Cancel if running, clean course output folder, and drop in-memory job record."""
    store = get_local_course_job_store()
    job = store.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    if job.status in (LocalJobStatus.PENDING, LocalJobStatus.PROCESSING):
        store.cancel_job(job_id, reason="Deleted by user")
        from lectora_backend.core.job_registry import get_local_pipeline

        handle = get_local_pipeline(job_id)
        if handle:
            handle.cancel_event.set()

    delete_course_output_tree(job.course_title)
    unregister_local_pipeline(job_id)

    store.remove(job_id)

    logger.info("[delete_job] Removed local job %s (title=%r)", job_id, job.course_title)
    return JSONResponse(
        content={"jobId": job_id, "status": "deleted", "message": "Job removed"},
    )


@router.get(
    "/{job_id}",
    summary="Poll job status and stage progress",
)
async def get_job(job_id: str) -> JSONResponse:
    store = get_local_course_job_store()
    job = store.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    error_detail: dict[str, Any] | None = None
    if job.status in (LocalJobStatus.FAILED, LocalJobStatus.CANCELLED) and job.error:
        error_detail = job.error

    return JSONResponse(content={
        "jobId": job.job_id,
        "status": job.status.value,
        "courseTitle": job.course_title,
        "courseType": job.course_type,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "stages": [s.to_dict() for s in job.stages],
        "errorDetail": error_detail,
    })


def _build_sse_payload(job_id: str, last_log_id: int) -> tuple[dict, int]:
    """Build a stage_update payload, returning only log entries newer than last_log_id.

    Returns (payload_dict, new_last_log_id) so the caller can track the delta
    position and avoid resending the same log entries on every poll tick.
    """
    store = get_local_course_job_store()
    job = store.get(job_id)
    if not job:
        return {"type": "error", "jobId": job_id, "message": "Job not found"}, last_log_id

    # Delta: only send log entries the client hasn't seen yet
    new_logs = [l for l in job.logs if l.id > last_log_id]
    new_last_log_id = new_logs[-1].id if new_logs else last_log_id

    payload = {
        "type": "stage_update",
        "jobId": job.job_id,
        "status": job.status.value,
        "updatedAt": job.updated_at,
        "stages": [s.to_dict() for s in job.stages],
        "error": job.error,
        "logs": [l.to_dict() for l in new_logs],
    }
    return payload, new_last_log_id


@router.get(
    "/{job_id}/events",
    summary="SSE stream — real-time pipeline stage updates",
)
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    store = get_local_course_job_store()
    if not store.get(job_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    async def _event_generator():
        terminal_statuses = {LocalJobStatus.COMPLETED, LocalJobStatus.FAILED}
        last_log_id = 0  # tracks the highest log ID sent to this client

        while True:
            if await request.is_disconnected():
                break

            payload, last_log_id = _build_sse_payload(job_id, last_log_id)
            yield f"data: {json.dumps(payload, default=str)}\n\n"

            job = store.get(job_id)
            if job and job.status in terminal_statuses:
                # Brief pause then send one final frame to flush remaining logs
                await asyncio.sleep(0.3)
                payload, last_log_id = _build_sse_payload(job_id, last_log_id)
                yield f"data: {json.dumps(payload, default=str)}\n\n"
                break

            await asyncio.sleep(_SSE_POLL_INTERVAL)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/{job_id}/course",
    summary="Course content from completed job",
)
async def get_course_content(
    job_id: str,
    course_slug: Annotated[str | None, Query(alias="courseSlug")] = None,
) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id, course_slug=course_slug)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    if job.status == LocalJobStatus.PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is still processing — please wait for it to complete.",
        )

    shared_state = _load_shared_state_dict(job, course_slug=course_slug)
    if not shared_state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course content not available — shared state not found in Azure or local storage",
        )

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}

    # Read sections directly from shared_state.json (single source of truth).
    # A2 writes a2_result.model_dump() to both generated_content.json AND
    # shared_state["agent_outputs"]["A2"] atomically — they are identical.
    generated_sections: list[dict] = a2_output.get("sections") or []

    if not generated_sections:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No generated sections found — ensure the pipeline completed successfully",
        )

    # ── Ensure every lesson has a visible L1 parent heading ──────────────────────
    # A2 only creates an is_parent_overview L1 section when the lesson is large
    # enough to warrant one; smaller lessons emit L2 subtopics only.  Without
    # this step the tree assembler would nest those subtopics at the root level
    # (no parent to adopt them), producing a flat list with no L1 groupings.
    generated_sections = _inject_missing_lesson_parent_sections(generated_sections)

    paragraphs_to_text = _paragraphs_to_text

    # ── Internal field names that must never appear as visible section titles ──────
    # These strings are JSON key names used internally by A2; they surface as
    # section headings when the LLM echoes a template key instead of real text.
    _INTERNAL_FIELD_NAMES: frozenset[str] = frozenset({
        "outline_lesson", "heading", "body_paragraphs", "section_id",
        "is_parent_overview", "word_count", "status", "level",
    })

    def _clean_title(raw_heading: str | None, outline_lesson: str | None, fallback: str) -> str:
        """Return a human-readable title, never an internal field name."""
        t = (raw_heading or "").strip()
        if not t or t.lower() in _INTERNAL_FIELD_NAMES:
            # Fall back to the lesson title if it's different and looks like real text
            lesson = (outline_lesson or "").strip()
            if lesson and lesson.lower() not in _INTERNAL_FIELD_NAMES:
                return lesson
            return fallback
        return t

    # ── Pull course-level metadata from shared_state (A0 extracted inputs) ───────
    extracted = shared_state.get("extracted_inputs", {})
    course_learning_objectives: list[str] = [
        str(lo) for lo in (extracted.get("learning_objectives") or [])
    ]
    content_sample: str = (extracted.get("content_sample") or "").strip()
    course_slug: str = shared_state.get("request", {}).get("courseStorageSlug", "")

    # ── Build flat section list then assemble into a level-based tree ──────────
    flat: list[dict] = []
    for i, sec in enumerate(generated_sections):
        level = min(int(sec.get("level") or 1), 3)  # cap at 3
        content_text = paragraphs_to_text(sec.get("body_paragraphs") or [])
        word_count = int(sec.get("word_count") or len(content_text.split()))

        # Build image list — A1 maps images to sections; A2 inherits them.
        section_images: list[dict] = []
        if course_slug:
            for img in (sec.get("images") or []):
                fname = img.get("media_filename") or img.get("fileName") or ""
                if not fname:
                    continue
                section_images.append({
                    "id": img.get("id") or fname,
                    "fileName": fname,
                    "blobPath": f"{course_slug}/images/{fname}",
                    "caption": img.get("caption") or None,
                    "altText": img.get("alt_text") or None,
                })

        flat.append({
            "id": sec.get("section_id") or f"sec_{i + 1}",
            "title": _clean_title(
                sec.get("heading"),
                sec.get("outline_lesson"),
                f"Section {i + 1}",
            ),
            "level": level,
            "sectionType": "content",
            "content": content_text,
            "paragraphs": sec.get("body_paragraphs") or [],
            "learningObjectives": [],
            "wordCount": word_count,
            "hasKnowledgeCheck": bool(sec.get("is_knowledge_check")),
            "order": i,
            "children": [],
            "images": section_images,
        })

    # ── Tree assembly: nest by heading level using a parent-stack ──────────────
    tree: list[dict] = []
    stack: list[tuple[int, dict]] = []  # (level, node)

    for node in flat:
        level = node["level"]
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
            node["parentId"] = stack[-1][1]["id"]
        else:
            tree.append(node)
        stack.append((level, node))

    # ── Pull LLM-generated front/back matter from A2 output in shared_state ─────
    # These are stored by A2 alongside sections so the editor shows the same
    # content as the rendered study_guide.docx (1.0 OVERVIEW + Conclusion).
    course_description: str = (a2_output.get("course_description") or "").strip()
    course_conclusion: str = (a2_output.get("course_conclusion") or "").strip()

    # Fallback for older runs that pre-date course_description storage:
    # trim content_sample to give a meaningful overview even without LLM text.
    if not course_description and content_sample:
        overview_words = content_sample.split()
        course_description = " ".join(overview_words[:500]) + ("…" if len(overview_words) > 500 else "")

    lo_text = "\n".join(
        f"{idx + 1}. {lo}" for idx, lo in enumerate(course_learning_objectives)
    )

    # ── Assemble full section tree: Overview → LOs → Content → Conclusion ──────
    special_sections: list[dict] = []
    if course_description:
        special_sections.append({
            "id": "course-overview",
            "title": "1.0 Overview",
            "level": 1,
            "sectionType": "overview",
            "content": course_description,
            "paragraphs": [{"type": "text", "content": course_description}],
            "learningObjectives": [],
            "wordCount": len(course_description.split()),
            "hasKnowledgeCheck": False,
            "order": -3,
            "children": [],
        })
    if course_learning_objectives:
        special_sections.append({
            "id": "course-learning-objectives",
            "title": "2.0 Learning Objectives",
            "level": 1,
            "sectionType": "learning-objectives",
            "content": lo_text,
            "paragraphs": [],
            "learningObjectives": course_learning_objectives,
            "wordCount": len(lo_text.split()),
            "hasKnowledgeCheck": False,
            "order": -2,
            "children": [],
        })

    conclusion_section: list[dict] = []
    if course_conclusion:
        conclusion_section.append({
            "id": "course-conclusion",
            "title": "Conclusion",
            "level": 1,
            "sectionType": "conclusion",
            "content": course_conclusion,
            "paragraphs": [{"type": "text", "content": course_conclusion}],
            "learningObjectives": [],
            "wordCount": len(course_conclusion.split()),
            "hasKnowledgeCheck": False,
            "order": 99999,
            "children": [],
        })

    full_tree = special_sections + tree + conclusion_section

    # ── Compute meta stats ─────────────────────────────────────────────────────
    total_words = sum(n["wordCount"] for n in flat)
    section_count = len(flat)
    chapter_count = sum(1 for n in flat if n["level"] == 1)
    read_minutes = max(1, total_words // 200)
    estimated_read = f"{read_minutes} min read" if read_minutes < 60 else f"{read_minutes // 60}h {read_minutes % 60}m"

    from datetime import datetime, timezone
    # Always resolve the title fresh from shared_state — the cached job record may
    # have been recovered before _persist_course_title wrote the correct value.
    resolved_title = (
        _course_title_from_shared_state(shared_state, course_slug)
        or job.course_title
        or a2_output.get("course_title")
        or "Untitled Course"
    )
    return JSONResponse(content={
        "jobId": job_id,
        "courseTitle": resolved_title,
        "courseType": job.course_type,
        "generatedAt": a2_output.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "meta": {
            "totalWordCount": total_words,
            "sectionCount": section_count,
            "chapterCount": chapter_count,
            "estimatedReadTime": estimated_read,
        },
        "sections": full_tree,
    })


class AIOperationPayload(BaseModel):
    operation: str
    section_id: str = Field(alias="sectionId")
    content: str | None = None
    context: dict[str, Any] | None = None
    user_prompt: str | None = Field(None, alias="userPrompt")

    model_config = {"populate_by_name": True}


class SaveSectionPayload(BaseModel):
    content: str
    section_type: str | None = Field(None, alias="sectionType")

    model_config = {"populate_by_name": True}


@router.patch(
    "/{job_id}/sections/{section_id}",
    summary="Persist FE-edited section content back to shared_state",
)
async def save_section_content(
    job_id: str,
    section_id: str,
    payload: SaveSectionPayload,
) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    state_path = _materialize_shared_state_to_disk(job)

    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    content = (payload.content or "").strip()
    stype = (payload.section_type or "content").strip()

    if stype == "overview":
        a2_output["course_description"] = content

    elif stype == "conclusion":
        a2_output["course_conclusion"] = content

    elif stype == "learning-objectives":
        # Parse "1. text\n2. text" back to plain list
        import re as _re
        los: list[str] = []
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _re.match(r"^\d+\.\s*(.+)$", line)
            los.append(m.group(1).strip() if m else line)
        extracted = shared_state.get("extracted_inputs") or {}
        extracted["learning_objectives"] = los
        shared_state["extracted_inputs"] = extracted

    else:
        # Regular content section — replace body_paragraphs with single plain-text block
        sections: list[dict] = a2_output.get("sections") or []
        updated = False
        for sec in sections:
            if sec.get("section_id") == section_id:
                sec["body_paragraphs"] = [{"type": "text", "content": content}]
                sec["word_count"] = len(content.split())
                updated = True
                break
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Section '{section_id}' not found in A2 output",
            )
        a2_output["sections"] = sections

    shared_state["agent_outputs"]["A2"] = a2_output
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

    return JSONResponse(content={"jobId": job_id, "sectionId": section_id, "status": "saved"})


@router.post(
    "/{job_id}/ai",
    summary="AI section operations — regenerate is fully implemented; others are stubs",
)
async def run_ai_operation(job_id: str, payload: AIOperationPayload) -> JSONResponse:
    import asyncio

    store = get_local_course_job_store()
    job = store.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    if payload.operation == "regenerate":
        try:
            loop = asyncio.get_event_loop()
            new_content = await loop.run_in_executor(
                None,
                _regenerate_section_sync,
                job_id,
                payload.section_id,
                payload.content or "",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Section regeneration failed: {exc}",
            ) from exc
        return JSONResponse(content={
            "jobId": job_id,
            "sectionId": payload.section_id,
            "operation": "regenerate",
            "status": "completed",
            "content": new_content,
            "processingTimeMs": 0,
        })

    if payload.operation == "rewrite":
        user_prompt = (payload.user_prompt or "").strip() or "Improve clarity and flow while preserving meaning."
        try:
            loop = asyncio.get_event_loop()
            new_content = await loop.run_in_executor(
                None,
                _rewrite_section_sync,
                job_id,
                payload.section_id,
                payload.content or "",
                user_prompt,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Section rewrite failed: {exc}",
            ) from exc
        return JSONResponse(content={
            "jobId": job_id,
            "sectionId": payload.section_id,
            "operation": "rewrite",
            "status": "completed",
            "content": new_content,
            "processingTimeMs": 0,
        })

    if payload.operation == "improve_tone":
        tone_prompt = (payload.user_prompt or "").strip() or "Professional, clear, and engaging"
        try:
            loop = asyncio.get_event_loop()
            new_content = await loop.run_in_executor(
                None,
                _improve_tone_sync,
                job_id,
                payload.section_id,
                payload.content or "",
                tone_prompt,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Tone improvement failed: {exc}",
            ) from exc
        return JSONResponse(content={
            "jobId": job_id,
            "sectionId": payload.section_id,
            "operation": "improve_tone",
            "status": "completed",
            "content": new_content,
            "processingTimeMs": 0,
        })

    if payload.operation == "summarize":
        try:
            loop = asyncio.get_event_loop()
            new_content = await loop.run_in_executor(
                None,
                _summarize_section_sync,
                job_id,
                payload.section_id,
                payload.content or "",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Section summarization failed: {exc}",
            ) from exc
        return JSONResponse(content={
            "jobId": job_id,
            "sectionId": payload.section_id,
            "operation": "summarize",
            "status": "completed",
            "content": new_content,
            "processingTimeMs": 0,
        })

    if payload.operation == "expand":
        try:
            loop = asyncio.get_event_loop()
            new_content = await loop.run_in_executor(
                None,
                _expand_section_sync,
                job_id,
                payload.section_id,
                payload.content or "",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Section expansion failed: {exc}",
            ) from exc
        return JSONResponse(content={
            "jobId": job_id,
            "sectionId": payload.section_id,
            "operation": "expand",
            "status": "completed",
            "content": new_content,
            "processingTimeMs": 0,
        })

    if payload.operation == "simplify":
        try:
            loop = asyncio.get_event_loop()
            new_content = await loop.run_in_executor(
                None,
                _simplify_section_sync,
                job_id,
                payload.section_id,
                payload.content or "",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Section simplification failed: {exc}",
            ) from exc
        return JSONResponse(content={
            "jobId": job_id,
            "sectionId": payload.section_id,
            "operation": "simplify",
            "status": "completed",
            "content": new_content,
            "processingTimeMs": 0,
        })

    # Unknown operation
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown AI operation: '{payload.operation}'",
    )


@router.get(
    "/{job_id}/training-outline",
    summary="Training Outline JSON for the TO review panel",
)
async def get_training_outline(job_id: str) -> JSONResponse:
    """Return FE-ready TO + rules from saved llm_to_outline.json (disk or Azure)."""
    from lectora_backend.api.routes.generate_to import build_fe_to_response_from_llm_outline

    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    llm_outline = _load_llm_outline_for_job(job)
    difficulty = "intermediate"
    state = _load_shared_state_dict(job)
    if state:
        difficulty = (state.get("course_difficulty") or difficulty).strip().lower()
        if not llm_outline or not llm_outline.get("sections"):
            llm_outline = state.get("llm_to_outline_classification") or llm_outline

    if not llm_outline or not llm_outline.get("sections"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Training Outline not found for this job",
        )

    response = build_fe_to_response_from_llm_outline(
        llm_outline,
        difficulty=difficulty,
        shared_state_path=job.shared_state_path,
    )
    return JSONResponse(content=response.model_dump(by_alias=True))


@router.get(
    "/{job_id}/artifacts",
    summary="List pipeline artifacts for a completed job",
)
async def list_artifacts(job_id: str) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    from lectora_backend.core.azure_course_artifacts import (
        is_azure_artifacts_enabled,
        list_json_artifacts_for_job,
    )

    artifacts: list[dict[str, Any]] = []

    if is_azure_artifacts_enabled():
        artifacts.extend(list_json_artifacts_for_job(job_id))

    if not artifacts and job.temp_dir and Path(job.temp_dir).exists():
        for p in sorted(Path(job.temp_dir).rglob("*")):
            if p.is_file():
                artifacts.append({
                    "name": p.name,
                    "path": str(p),
                    "size": p.stat().st_size,
                    "type": "docx" if p.suffix == ".docx" else "json",
                    "source": "local",
                })

    return JSONResponse(content={"jobId": job_id, "artifacts": artifacts})


@router.get(
    "/{job_id}/artifacts/download",
    summary="Download the generated study guide docx (always rebuilt from latest shared state)",
)
async def download_artifact(
    job_id: str,
    course_slug: Annotated[str | None, Query(alias="courseSlug")] = None,
) -> FileResponse:
    import asyncio
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id, course_slug=course_slug)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    if job.status != LocalJobStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job not completed (status: {job.status.value})",
        )

    state_path = _materialize_shared_state_to_disk(job, course_slug=course_slug)

    # Rebuild DOCX from latest shared_state so any FE edits / regenerated
    # sections are included. render_study_guide_from_state uses stored
    # course_description/conclusion (no extra LLM calls).
    try:
        loop = asyncio.get_event_loop()
        docx_path = await loop.run_in_executor(
            None,
            render_study_guide_from_state,
            str(state_path),
        )
        store.update_study_guide_path(job_id, docx_path)
    except Exception as exc:
        # Fall back to previously built file if rebuild fails
        docx_path = job.study_guide_path
        if not docx_path or not Path(docx_path).exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"DOCX rebuild failed and no cached file available: {exc}",
            ) from exc

    return FileResponse(
        path=docx_path,
        filename=f"course_{job_id[:8]}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


class SaveToAzurePayload(BaseModel):
    course_title: str | None = Field(None, alias="courseTitle")
    course_slug: str | None = Field(None, alias="courseSlug")
    section_order: list[str] | None = Field(None, alias="sectionOrder")

    model_config = {"populate_by_name": True}


def _apply_section_order_to_shared_state(
    state_path: "Path", section_order: list[str]
) -> None:
    """Reorder A2 sections in shared_state.json to match the given ID list.

    Only IDs that exist in a2_output['sections'] are considered. Special
    frontend-only sections (course-overview, etc.) are silently skipped since
    they don't live in the A2 sections array.
    """
    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2", {})
    sections: list[dict] = a2_output.get("sections") or []
    if not sections or not section_order:
        return

    by_id = {s.get("section_id"): s for s in sections if s.get("section_id")}
    if not by_id:
        return

    # Build reordered list from provided IDs, preserving only known A2 sections
    reordered = [by_id[sid] for sid in section_order if sid in by_id]
    # Append any A2 sections that were not present in section_order (safety net)
    ordered_set = set(section_order)
    remaining = [s for s in sections if s.get("section_id") not in ordered_set]
    reordered.extend(remaining)

    shared_state["agent_outputs"]["A2"]["sections"] = reordered
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)


@router.post(
    "/{job_id}/artifacts/save-to-azure",
    summary="Upload the generated DOCX to Azure Blob Storage",
)
async def save_artifact_to_azure(
    job_id: str,
    payload: SaveToAzurePayload | None = None,
) -> JSONResponse:
    from lectora_backend.config import settings as _settings

    if not _settings.is_azure_storage_configured():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "azure_not_configured",
                "message": (
                    "Azure Blob Storage is not configured. "
                    "Set AZURE_STORAGE_CONNECTION_STRING in .env to enable this feature."
                ),
            },
        )

    body = payload or SaveToAzurePayload()
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(
        store,
        job_id,
        course_slug=body.course_slug,
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    if job.status != LocalJobStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job not completed (status: {job.status.value})",
        )

    state_path = _materialize_shared_state_to_disk(job, course_slug=body.course_slug)

    if body.course_title and body.course_title.strip():
        _apply_course_title_to_shared_state(state_path, body.course_title)
        store.update_course_title(job_id, body.course_title.strip())
        job.course_title = body.course_title.strip()

    if body.section_order:
        _apply_section_order_to_shared_state(state_path, body.section_order)

    # Rebuild DOCX from latest shared_state so any FE edits are included.
    try:
        loop = asyncio.get_event_loop()
        docx_path = await loop.run_in_executor(
            None,
            render_study_guide_from_state,
            str(state_path),
        )
        store.update_study_guide_path(job_id, docx_path)
    except Exception as exc:
        docx_path = job.study_guide_path
        if not docx_path or not Path(docx_path).exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"DOCX rebuild failed: {exc}",
            ) from exc

    from lectora_backend.core.local_artifact_sync import sync_local_artifacts_to_azure

    sync_result = sync_local_artifacts_to_azure(
        job_id=job_id,
        course_title=job.course_title,
        shared_state_path=str(state_path),
        study_guide_path=docx_path,
    )
    if sync_result.get("skipped"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "azure_not_configured",
                "message": "Azure Blob Storage is not configured.",
            },
        )
    if sync_result.get("uploaded", 0) == 0 and sync_result.get("errors"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "upload_failed",
                "message": f"Azure upload failed: {sync_result['errors'][0]}",
            },
        )

    blob_root = sync_result.get("blobRoot", "")
    container = sync_result.get(
        "generatedCoursesContainer",
        _settings.generated_courses_container_name,
    )
    blob_path = f"{blob_root}/output/study_guide.docx"
    file_name = "study_guide.docx"

    logger.info(
        "[save_to_azure] Synced %s artifact(s) for job %s → %s/%s",
        sync_result.get("uploaded", 0), job_id, container, blob_root,
    )
    return JSONResponse(content={
        "status":        "uploaded",
        "jobId":         job_id,
        "fileName":      file_name,
        "blobPath":      blob_path,
        "containerName": container,
        "savedAt":       datetime.now(timezone.utc).isoformat(),
    })
