"""Job creation, status, retry, artifact, course-content, and AI-operation endpoints."""
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lectora_backend.api.schemas.artifact_schemas import ArtifactListResponse, ArtifactSummary
from lectora_backend.api.schemas.course_schemas import (
    AIOperationRequest,
    AIOperationResponse,
    ArtifactDownloadResponse,
    CourseContentMeta,
    CourseContentResponse,
    CourseSectionSchema,
    SectionImageSchema,
)
from lectora_backend.api.schemas.job_schemas import (
    JobCreateRequest,
    JobCreateResponse,
    JobDetailResponse,
    JobErrorDetail,
    RetryRequest,
    RetryResponse,
    StageProgressResponse,
)
from lectora_backend.dependencies import get_db_session
from lectora_backend.models.job_enums import JobStatus, PipelineStep, StageStatus, ValidationOutcome
from lectora_backend.repositories.job_repository import JobRepository
from lectora_backend.core.blob_layout import build_blob_layout_for_course
from lectora_backend.core.course_storage import sanitize_course_slug
from lectora_backend.core.storage_cleanup import delete_course_output_tree
from lectora_backend.core.state_manager import StateManager
from lectora_backend.core.queue_publisher import get_queue_publisher
from lectora_backend.models.constants import PIPELINE_ORDER, STAGE_ORDER

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Error response helpers ─────────────────────────────────────────────────────

def _missing_input_response(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "code": "MISSING_REQUIRED_INPUT",
                "message": message,
                "stage": None,
                "retryable": False,
            }
        },
    )


def _job_init_error_response(message: str, retryable: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "JOB_INITIALIZATION_FAILED",
                "message": message,
                "stage": None,
                "retryable": retryable,
            }
        },
    )


# ── Mapping helpers ────────────────────────────────────────────────────────────

def _map_job_error(error_detail: str | None) -> JobErrorDetail | None:
    if not error_detail:
        return None

    fallback = JobErrorDetail(
        code="MALFORMED_ERROR_DETAIL",
        message=error_detail,
        stage=None,
        retryable=False,
    )

    try:
        payload = json.loads(error_detail)
    except json.JSONDecodeError:
        return fallback

    if not isinstance(payload, dict):
        return fallback

    stage = payload.get("stage")
    try:
        parsed_stage = PipelineStep(stage) if stage else None
    except ValueError:
        parsed_stage = None

    return JobErrorDetail(
        code=str(payload.get("code") or fallback.code),
        message=str(payload.get("message") or fallback.message),
        stage=parsed_stage,
        retryable=bool(payload.get("retryable", fallback.retryable)),
    )


def _map_job_detail(job) -> JobDetailResponse:
    # Use precomputed O(1) lookup instead of PIPELINE_ORDER.index() which raises
    # ValueError on unknown stages and is O(n) per call.
    ordered_stage_progress = sorted(
        job.stage_progress,
        key=lambda item: STAGE_ORDER.get(item.stage_id, len(PIPELINE_ORDER)),
    )

    return JobDetailResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        stages=[
            StageProgressResponse(
                stage=stage.stage_id,
                status=stage.status,
                started_at=stage.started_at,
                completed_at=stage.completed_at,
                outcome=ValidationOutcome(stage.validation_outcome.value)
                if stage.validation_outcome
                else None,
            )
            for stage in ordered_stage_progress
        ],
        error=_map_job_error(
            next(
                (
                    stage.error_detail
                    for stage in ordered_stage_progress
                    if stage.error_detail
                ),
                None,
            )
        ),
    )


_ARTIFACT_STAGE_MAP = {
    "requestSpec": PipelineStep.A0.value,
    "provenanceLog": PipelineStep.A0.value,
    "llmToOutline": PipelineStep.A0.value,
    "courseSpec": PipelineStep.A1.value,
    "a1Status": PipelineStep.A1.value,
    "a1Marker": PipelineStep.A1.value,
    "images": PipelineStep.A0.value,
    "s1Validation": PipelineStep.S1.value,
    "sectionMap": PipelineStep.A2.value,
    "generatedContent": PipelineStep.A2.value,
    "generatedStudyGuide": PipelineStep.A2.value,
    "pipelineSharedState": PipelineStep.A2.value,
}


def _map_artifacts(job) -> list[ArtifactSummary]:
    state = StateManager().load(job.job_id, blob_path=job.shared_state_blob_path)
    artifact_refs = state.get("artifactRefs", {})
    created_at = job.updated_at or job.created_at
    artifacts: list[ArtifactSummary] = []

    for artifact_type, value in artifact_refs.items():
        stage = _ARTIFACT_STAGE_MAP.get(artifact_type, "")

        if isinstance(value, dict) and value.get("blobPath"):
            artifacts.append(
                ArtifactSummary(
                    type=artifact_type,
                    blob_path=str(value["blobPath"]),
                    stage=stage,
                    is_latest=True,
                    created_at=created_at,
                )
            )
            continue

        if artifact_type == "images" and isinstance(value, list):
            for item in value:
                blob_path = item.get("blobPath")
                if not blob_path:
                    continue
                image_name = item.get("fileName") or artifact_type
                artifacts.append(
                    ArtifactSummary(
                        type=f"image:{image_name}",
                        blob_path=str(blob_path),
                        stage=stage,
                        is_latest=True,
                        created_at=created_at,
                    )
                )

    return artifacts


# ── Job creation ───────────────────────────────────────────────────────────────

@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    payload: JobCreateRequest,
    session: Session = Depends(get_db_session),
) -> JobCreateResponse:
    if payload.inputs.timed_outline is None:
        logger.warning(
            "[create_job] timedOutline not provided for job — pipeline will use Scenario C (algorithmic KC)."
        )

    job_id = f"j-{uuid.uuid4().hex}"
    actor = "system"
    study_guide_blob_path = payload.inputs.study_guide.blob_path
    if not study_guide_blob_path or not study_guide_blob_path.strip():
        return _missing_input_response("studyGuide.blobPath is required.")

    blob_layout = build_blob_layout_for_course(payload.course_title, job_id=job_id)
    course_slug = sanitize_course_slug(payload.course_title)

    repository = JobRepository(session)
    repository.create_job(
        job_id=job_id,
        course_title=payload.course_title,
        course_type=payload.course_type,
        requested_by=actor,
        shared_state_blob_path=blob_layout.shared_state_blob_path,
        commit=False,
    )

    timestamp = datetime.now(timezone.utc).isoformat()

    initial_state = {
        "run": {
            "jobId": job_id,
            "runAttempt": 1,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "triggeredBy": actor,
        },
        "request": {
            "courseTitle": payload.course_title,
            "courseStorageSlug": course_slug,
            "courseType": payload.course_type,
            "requestedBy": actor,
            "normalizedAt": None,
        },
        "inputManifest": {
            "courseBrief": (
                {"blobPath": payload.inputs.course_brief.blob_path}
                if payload.inputs.course_brief
                else None
            ),
            "timedOutline": (
                {"blobPath": payload.inputs.timed_outline.blob_path}
                if payload.inputs.timed_outline
                else None
            ),
            "studyGuide": {"blobPath": payload.inputs.study_guide.blob_path},
            "examReference": (
                {"blobPath": payload.inputs.exam_reference.blob_path}
                if payload.inputs.exam_reference
                else None
            ),
            "complianceNotes": (
                {"blobPath": payload.inputs.compliance_notes.blob_path}
                if payload.inputs.compliance_notes
                else None
            ),
        },
        "artifactRefs": {},
        "blobLayout": blob_layout.to_dict(),
        "retryHistory": [],
        "toOverride": payload.to_override,
        "stageExecutionState": {
            stage.value: {"status": StageStatus.PENDING.value}
            for stage in PIPELINE_ORDER
        },
    }

    state_manager = StateManager()
    try:
        state_manager.initialize(
            job_id=job_id,
            initial_state=initial_state,
            blob_path=blob_layout.shared_state_blob_path,
        )
    except Exception as exc:
        session.rollback()
        try:
            state_manager.delete(
                job_id, blob_path=blob_layout.shared_state_blob_path)
        except Exception:
            pass
        return _job_init_error_response(
            f"Failed to initialize job resources: {exc}",
            True,
        )

    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        try:
            state_manager.delete(
                job_id, blob_path=blob_layout.shared_state_blob_path)
        except Exception:
            pass
        return _job_init_error_response(
            f"Failed to persist initialized job: {exc}",
            True,
        )

    try:
        await get_queue_publisher().enqueue(job_id)
    except Exception as exc:
        logger.exception("[create_job] Failed to enqueue job %s", job_id)
        repository.mark_job_failed(
            job_id=job_id,
            code="JOB_INITIALIZATION_FAILED",
            message="Failed to enqueue job — see server logs.",
            retryable=True,
        )
        return _job_init_error_response(
            "Failed to enqueue job — please retry.",
            True,
        )

    return JobCreateResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
    )


# ── Job detail ─────────────────────────────────────────────────────────────────

@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: str,
    session: Session = Depends(get_db_session),
) -> JobDetailResponse:
    repository = JobRepository(session)
    job = repository.get_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    return _map_job_detail(job)


# ── Job lookup by course slug ──────────────────────────────────────────────────

@router.get("/by-course-slug/{course_slug}")
async def get_job_by_course_slug(
    course_slug: str,
    session: Session = Depends(get_db_session),
) -> dict:
    """Return the most recent job for a given course slug (used by Asset Library to open DOCX in editor)."""
    repository = JobRepository(session)
    job = repository.get_latest_job_by_course_slug(course_slug)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job found for course slug '{course_slug}'.",
        )
    return {
        "jobId": job.job_id,
        "status": job.status.value,
        "courseTitle": job.course_title,
    }


# ── Job deletion ───────────────────────────────────────────────────────────────

@router.delete("/{job_id}", status_code=status.HTTP_200_OK)
async def delete_job(
    job_id: str,
    session: Session = Depends(get_db_session),
) -> dict[str, str]:
    """Remove job metadata and delete course output artifacts from blob storage."""
    repository = JobRepository(session)
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found."
        )

    if job.status in (JobStatus.PENDING, JobStatus.PROCESSING):
        repository.update_job_status(job_id, JobStatus.CANCELLED)

    # delete_course_output_tree already removes the {slug}/ prefix from Azure
    # and local filesystem — no need for a separate state_manager cleanup call.
    delete_course_output_tree(job.course_title)

    if not repository.delete_job(job_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found."
        )

    logger.info("[delete_job] Deleted job %s (course=%r)", job_id, job.course_title)
    return {"jobId": job_id, "status": "deleted", "message": "Job and artifacts removed"}


# ── Retry ──────────────────────────────────────────────────────────────────────

@router.post("/{job_id}/retry", response_model=RetryResponse)
async def retry_job(
    job_id: str,
    payload: RetryRequest,
    session: Session = Depends(get_db_session),
) -> RetryResponse:
    repository = JobRepository(session)
    job = repository.get_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Guard: only FAILED or CANCELLED jobs can be retried
    if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is in status '{job.status.value}' and cannot be retried. "
                   "Only FAILED or CANCELLED jobs are retryable.",
        )

    job = repository.record_retry(
        job_id=job_id,
        from_stage=payload.from_stage,
        section_id=payload.section_id,
        overrides=payload.overrides,
        triggered_by="system",
    )

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Re-enqueue so the worker actually processes the retry.
    try:
        await get_queue_publisher().enqueue(job_id)
    except Exception as exc:
        logger.exception(
            "[retry_job] Failed to enqueue retry for job %s", job_id
        )
        repository.update_job_status(job_id, JobStatus.FAILED)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue job for retry — see server logs.",
        ) from exc

    return RetryResponse(
        job_id=job_id,
        status=job.status,
        retry_from_stage=payload.from_stage,
        section_id=payload.section_id,
        overrides=payload.overrides,
    )


# ── Artifacts ──────────────────────────────────────────────────────────────────

@router.get("/{job_id}/artifacts", response_model=ArtifactListResponse)
async def get_job_artifacts(
    job_id: str,
    session: Session = Depends(get_db_session),
) -> ArtifactListResponse:
    repository = JobRepository(session)
    job = repository.get_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    return ArtifactListResponse(
        job_id=job_id,
        artifacts=_map_artifacts(job),
    )


# ── Course content ─────────────────────────────────────────────────────────────

def _build_a2_content_lookup(
    a2_sections: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Build two lookup dicts from A2's flat generated sections list."""
    by_section_id: dict[str, dict] = {}
    by_lesson_overview: dict[str, dict] = {}
    for sec in a2_sections or []:
        sid = sec.get("section_id") or sec.get("id", "")
        if sid:
            by_section_id[sid] = sec
        if sec.get("is_parent_overview") or sec.get("level") == 1:
            lesson = (sec.get("outline_lesson") or sec.get("heading") or "").strip()
            if lesson:
                by_lesson_overview[lesson] = sec
    return by_section_id, by_lesson_overview


def _build_section(
    raw: dict,
    order: int,
    level: int,
    parent_id: str | None,
    a2_by_id: dict[str, dict] | None = None,
    a2_by_lesson: dict[str, dict] | None = None,
    course_slug: str = "",
) -> CourseSectionSchema:
    """Recursively convert an enriched_sections dict to CourseSectionSchema.

    When A2 content is available (a2_by_id / a2_by_lesson) the generated text
    is merged in so the editor gets the real course body, not the raw outline.
    """
    section_id = raw.get("id") or str(uuid.uuid4())
    title = raw.get("title", "Untitled")

    a2_sec: dict = {}
    if a2_by_id and section_id in a2_by_id:
        a2_sec = a2_by_id[section_id]
    elif a2_by_lesson and title.strip() in a2_by_lesson:
        a2_sec = a2_by_lesson[title.strip()]

    content = (
        a2_sec.get("content")
        or raw.get("content")
        or raw.get("summary", "")
    )
    objectives = raw.get("learning_objectives", raw.get("objectives", []))
    has_kc = bool(
        raw.get("has_knowledge_check")
        or (a2_sec and a2_sec.get("has_knowledge_check"))
    )
    word_count = a2_sec.get("word_count") or (len(content.split()) if content else 0)

    # Build image list — images are mapped to sections by A1 and propagated
    # through section_mapper. Only include when a course_slug is available so
    # the storage URL can be constructed.
    raw_images: list[dict] = raw.get("images") or []
    images: list[SectionImageSchema] = []
    if course_slug:
        for img in raw_images:
            fname = img.get("media_filename") or img.get("fileName") or ""
            if not fname:
                continue
            images.append(SectionImageSchema(
                id=img.get("id") or fname,
                file_name=fname,
                blob_path=f"{course_slug}/images/{fname}",
                caption=img.get("caption") or None,
                alt_text=img.get("alt_text") or None,
            ))

    children_raw = raw.get("subtopics", raw.get("chapters", raw.get("children", [])))
    children: list[CourseSectionSchema] = [
        _build_section(child, i, level + 1, section_id, a2_by_id, a2_by_lesson, course_slug)
        for i, child in enumerate(children_raw or [])
    ]

    return CourseSectionSchema(
        id=section_id,
        title=title,
        level=min(level, 3),
        content=content,
        learning_objectives=objectives if isinstance(objectives, list) else [],
        word_count=word_count,
        has_knowledge_check=has_kc,
        estimated_duration=raw.get("estimated_duration"),
        order=order,
        parent_id=parent_id,
        children=children,
        images=images,
    )


def _sum_words_deep(sections: list[CourseSectionSchema]) -> int:
    """Recursively sum word counts across all nesting levels."""
    total = 0
    for s in sections:
        total += s.word_count
        if s.children:
            total += _sum_words_deep(s.children)
    return total


def _state_to_course_content(
    job_id: str,
    course_title: str,
    course_type: str,
    state: dict,
) -> CourseContentResponse:
    """Extract course structure from shared_state and return as CourseContentResponse."""
    agent_outputs = state.get("agent_outputs", {})

    a2_raw = agent_outputs.get("A2") or {}
    a2_sections: list[dict] = a2_raw.get("sections") or []
    a2_by_id, a2_by_lesson = _build_a2_content_lookup(a2_sections)

    enriched = (
        agent_outputs.get("section_map", {}).get("enriched_sections")
        or agent_outputs.get("A1", {}).get("course_spec", {}).get("sections")
        or []
    )

    course_slug = state.get("request", {}).get("courseStorageSlug", "")

    sections: list[CourseSectionSchema] = [
        _build_section(raw, i, 1, None, a2_by_id or None, a2_by_lesson or None, course_slug)
        for i, raw in enumerate(enriched or [])
    ]

    total_words = _sum_words_deep(sections)
    chapter_count = sum(len(s.children) for s in sections)
    read_min = max(1, math.ceil(total_words / 200))
    generated_at = (
        state.get("run", {}).get("updatedAt")
        or datetime.now(timezone.utc).isoformat()
    )

    return CourseContentResponse(
        job_id=job_id,
        course_title=course_title,
        course_type=course_type,
        generated_at=generated_at,
        meta=CourseContentMeta(
            total_word_count=total_words,
            section_count=len(sections),
            chapter_count=chapter_count,
            estimated_read_time=f"{read_min} min",
        ),
        sections=sections,
    )


@router.get("/{job_id}/course", response_model=CourseContentResponse)
async def get_job_course_content(
    job_id: str,
    course_slug: Annotated[str | None, Query(alias="courseSlug")] = None,
    session: Session = Depends(get_db_session),
) -> CourseContentResponse:
    """Return structured course content for the editor (COMPLETED jobs only)."""
    repository = JobRepository(session)
    job = repository.get_job(job_id)
    if job is None:
        # Fallback for dev-generated jobs (hex UUID = created by local_course_job_store, never in SQL)
        if '-' not in job_id:
            from lectora_backend.api.routes.local_jobs import get_course_content as _local_get_course
            return await _local_get_course(job_id, course_slug=course_slug)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Course content not yet available — job status is {job.status.value}.",
        )

    try:
        state = StateManager().load(job_id, blob_path=job.shared_state_blob_path)
    except Exception as exc:
        logger.warning("Failed to load shared state for job %s: %s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not retrieve course content. Artifact storage may be unavailable.",
        ) from exc

    return _state_to_course_content(job_id, job.course_title, job.course_type, state)


# ── AI section operations ──────────────────────────────────────────────────────


def _set_trace_context_for_job(job_id: str, job) -> None:
    """Tag editor AI traces with job_id, doc_name, and source refs from shared state."""
    from pathlib import Path

    from lectora_backend.core.state_manager import StateManager
    from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

    doc_name = ""
    source_refs: list[str] = []
    try:
        state = StateManager().load(job_id, blob_path=job.shared_state_blob_path)
        source_refs = list(state.get("source_file_paths") or [])
        study_guide_blob = (
            (state.get("inputManifest") or {}).get("studyGuide") or {}
        ).get("blobPath") or ""
        if study_guide_blob:
            if study_guide_blob not in source_refs:
                source_refs.insert(0, study_guide_blob)
            doc_name = Path(study_guide_blob).stem
        if not doc_name:
            course_title = (state.get("request") or {}).get("courseTitle") or ""
            if course_title.strip():
                doc_name = course_title.strip().replace(" ", "_")
    except Exception as exc:
        logger.debug("[%s] trace context: could not load shared state: %s", job_id, exc)

    if not doc_name and job.course_title:
        doc_name = job.course_title.strip().replace(" ", "_")
    if not doc_name:
        doc_name = job_id[:8]

    set_run_context(job_id, doc_name, source_refs=source_refs)


_AI_OPERATION_PROMPTS: dict[str, str] = {
    "summarize": (
        "You are an expert course content editor. "
        "Summarize the following course section into a concise version that retains all key "
        "learning points, facts, and concepts. Target roughly 40–60% of the original length. "
        "Use clear, direct language. "
        "Output only the summarized content — no preamble, labels, or explanation."
    ),
    "expand": (
        "You are an expert course content writer. "
        "Expand the following course section by adding more depth, concrete examples, "
        "and elaboration on key concepts. Maintain the same educational tone and style. "
        "Only deepen what is already present — do not introduce unrelated topics. "
        "Output only the expanded content — no preamble, labels, or explanation."
    ),
    "simplify": (
        "You are an expert course content editor. "
        "Simplify the following course section by using plainer language, shorter sentences, "
        "and a more accessible writing style. Avoid jargon where possible; "
        "when technical terms are necessary, briefly explain them. "
        "Preserve all factual content and learning points exactly. "
        "Output only the simplified content — no preamble, labels, or explanation."
    ),
    "rewrite": (
        "You are an expert course content writer. "
        "Rewrite the provided course section following the user's instructions exactly. "
        "Preserve all factual content and learning points. "
        "Output only the rewritten section content — no preamble, labels, or explanation."
    ),
    "improve_tone": (
        "You are an expert course content editor specialising in tone and style. "
        "Rewrite the provided course section in the requested tone and style. "
        "Preserve all factual content and learning points. "
        "Output only the revised content — no preamble, labels, or explanation."
    ),
    "regenerate": (
        "You are an expert course content writer. "
        "Fully rewrite the following course section, creating fresh, engaging content "
        "that covers the same topics and learning points with a new perspective. "
        "Output only the rewritten content — no preamble, labels, or explanation."
    ),
}


@router.post("/{job_id}/ai", response_model=AIOperationResponse)
async def perform_ai_operation(
    job_id: str,
    payload: AIOperationRequest,
    session: Session = Depends(get_db_session),
) -> AIOperationResponse:
    """Run an AI operation (summarize / expand / simplify / rewrite / improve_tone / regenerate)
    on a specific section's content using Azure OpenAI."""
    import asyncio as _asyncio

    from lectora_backend.pipeline.shared_llm_config.llm import LLMConfig
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment

    repository = JobRepository(session)
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    system_prompt = _AI_OPERATION_PROMPTS.get(payload.operation)
    if not system_prompt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown AI operation: '{payload.operation}'",
        )

    content = (payload.content or "").strip()
    user_prompt = (payload.user_prompt or "").strip()

    if payload.operation in ("rewrite", "improve_tone") and user_prompt:
        label = "REWRITE INSTRUCTIONS" if payload.operation == "rewrite" else "DESIRED TONE/STYLE"
        user_msg = f"CURRENT SECTION CONTENT:\n{content}\n\n{label}:\n{user_prompt}"
    else:
        user_msg = f"COURSE SECTION CONTENT:\n{content}"

    _set_trace_context_for_job(job_id, job)

    editor_config = LLMConfig(
        deployment=get_deployment("A2"),
        temperature=0.35,
        max_tokens=2000,
    )

    t0 = time.monotonic()
    try:
        loop = _asyncio.get_event_loop()
        result_content = await loop.run_in_executor(
            None,
            lambda: llm_chat(system_prompt, user_msg, config=editor_config, agent="editor"),
        )
    except Exception as exc:
        logger.exception("[%s] AI operation '%s' failed: %s", job_id, payload.operation, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AI operation failed: {exc}",
        ) from exc

    elapsed_ms = int((time.monotonic() - t0) * 1_000)
    result_content = (result_content or content).strip()

    return AIOperationResponse(
        section_id=payload.section_id,
        operation=payload.operation,
        content=result_content,
        processing_time_ms=elapsed_ms,
    )


# ── Artifact download URL ──────────────────────────────────────────────────────

@router.get("/{job_id}/artifacts/download", response_model=ArtifactDownloadResponse)
async def get_artifact_download_url(
    job_id: str,
    session: Session = Depends(get_db_session),
) -> ArtifactDownloadResponse:
    """Return a download URL for the generated study_guide.docx.

    TODO: replace blob_path with a short-lived SAS URL via
    BlobRepository.generate_sas_url() when that helper is available.
    """
    repository = JobRepository(session)
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    try:
        state = StateManager().load(job_id, blob_path=job.shared_state_blob_path)
        artifact_refs = state.get("artifactRefs", {})
        generated_docx = artifact_refs.get("generatedStudyGuide", {})
        blob_path = (
            generated_docx.get("blobPath")
            if isinstance(generated_docx, dict)
            else None
        )
    except Exception as exc:
        logger.warning(
            "Could not resolve artifact blob path for job %s: %s", job_id, exc
        )
        blob_path = None

    if not blob_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DOCX artifact not found. The job may not be complete.",
        )

    return ArtifactDownloadResponse(
        url=blob_path,
        filename="study_guide.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
