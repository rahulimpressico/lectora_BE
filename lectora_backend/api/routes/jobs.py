"""Job creation, status, retry, artifact, course-content, and AI-operation endpoints."""
import json
import logging
import math
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
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
from lectora_backend.core.blob_layout import build_blob_layout_from_input_blob
from lectora_backend.core.state_manager import StateManager
from lectora_backend.core.queue_publisher import QueuePublisher
from lectora_backend.models.constants import PIPELINE_ORDER

logger = logging.getLogger(__name__)


router = APIRouter()


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
    ordered_stage_progress = sorted(
        job.stage_progress,
        key=lambda item: PIPELINE_ORDER.index(item.stage_id),
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
                (stage.error_detail for stage in ordered_stage_progress if stage.error_detail), None)
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


@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    payload: JobCreateRequest,
    session: Session = Depends(get_db_session),
) -> JobCreateResponse:
    # timedOutline is strongly recommended — the pipeline's Section Mapper
    # relies on it to map course sections to TO lessons. Without it the worker
    # will fall back to Scenario C (algorithmic KC placement), which may reduce
    # content quality. We log a warning but do NOT reject the request.
    if payload.inputs.timed_outline is None:
        logger.warning(
            "[create_job] timedOutline not provided for job — pipeline will use Scenario C (algorithmic KC)."
        )

    job_id = f"j-{uuid4().hex[:8]}"
    actor = "system"
    study_guide_blob_path = payload.inputs.study_guide.blob_path
    if not study_guide_blob_path or not study_guide_blob_path.strip():
        return _missing_input_response("studyGuide.blobPath is required.")

    blob_layout = build_blob_layout_from_input_blob(
        study_guide_blob_path,
        job_id,
    )

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
        # User-edited TO from the three-panel review step.  When present the
        # pipeline_adapter patches llm_to_outline_classification before A1 runs.
        "toOverride": payload.to_override,
        "stageExecutionState": {
            "A0": {"status": StageStatus.PENDING.value},
            "A1": {"status": StageStatus.PENDING.value},
            "S1": {"status": StageStatus.PENDING.value},
            "A2": {"status": StageStatus.PENDING.value},
            "A3": {"status": StageStatus.PENDING.value},
            "A4": {"status": StageStatus.PENDING.value},
            "A5": {"status": StageStatus.PENDING.value},
            "S2": {"status": StageStatus.PENDING.value},
            "A6": {"status": StageStatus.PENDING.value},
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
        publisher = QueuePublisher()
        await publisher.enqueue(job_id)
    except Exception as exc:
        repository.mark_job_failed(
            job_id=job_id,
            code="JOB_INITIALIZATION_FAILED",
            message=f"Failed to enqueue job: {exc}",
            retryable=True,
        )
        return _job_init_error_response(
            f"Failed to enqueue job: {exc}",
            True,
        )

    return JobCreateResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
    )


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


@router.post("/{job_id}/retry", response_model=RetryResponse)
async def retry_job(
    job_id: str,
    payload: RetryRequest,
    session: Session = Depends(get_db_session),
) -> RetryResponse:
    repository = JobRepository(session)
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

    return RetryResponse(
        job_id=job_id,
        status=job.status,
        retry_from_stage=payload.from_stage,
        section_id=payload.section_id,
        overrides=payload.overrides,
    )


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
    """
    Build two lookup dicts from A2's flat generated sections list.

    Returns:
        by_section_id: maps enriched_sections subtopic id → a2 section dict
        by_lesson_overview: maps TO lesson title → a2 parent overview section dict
    """
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
    counter: list[int],
    a2_by_id: dict[str, dict] | None = None,
    a2_by_lesson: dict[str, dict] | None = None,
) -> CourseSectionSchema:
    """Recursively convert enriched_sections dict to CourseSectionSchema.

    When A2 content is available (a2_by_id / a2_by_lesson) the generated text
    is merged in so the editor gets the real course body, not the raw outline.
    """
    import uuid as _uuid
    section_id = raw.get("id") or str(_uuid.uuid4())
    title = raw.get("title", "Untitled")

    # Try to pull A2-generated body text
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

    children_raw = raw.get("subtopics", raw.get("chapters", raw.get("children", [])))
    children: list[CourseSectionSchema] = []
    for i, child in enumerate(children_raw or []):
        children.append(
            _build_section(child, i, level + 1, section_id, counter, a2_by_id, a2_by_lesson)
        )

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
    )


def _state_to_course_content(
    job_id: str,
    course_title: str,
    course_type: str,
    state: dict,
) -> CourseContentResponse:
    """Extract course structure from shared_state and return as CourseContentResponse.

    Priority:
      1. A2 generated sections merged into enriched_sections hierarchy (full content).
      2. enriched_sections alone (structure, no body text) if A2 hasn't run.
      3. A1 course_spec as last resort.
    """
    agent_outputs = state.get("agent_outputs", {})

    # A2 generated content (flat list written by content_writer.generate_all_sections)
    a2_raw = agent_outputs.get("A2") or {}
    a2_sections: list[dict] = a2_raw.get("sections") or []
    a2_by_id, a2_by_lesson = _build_a2_content_lookup(a2_sections)

    # Structural hierarchy (enriched or course_spec)
    enriched = (
        agent_outputs.get("section_map", {}).get("enriched_sections")
        or agent_outputs.get("A1", {}).get("course_spec", {}).get("sections")
        or []
    )

    sections: list[CourseSectionSchema] = []
    counter: list[int] = [0]
    for i, raw in enumerate(enriched or []):
        sections.append(
            _build_section(raw, i, 1, None, counter, a2_by_id or None, a2_by_lesson or None)
        )

    total_words = sum(
        s.word_count + sum(c.word_count for c in s.children)
        for s in sections
    )
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
    session: Session = Depends(get_db_session),
) -> CourseContentResponse:
    """Return structured course content for the editor.

    Reads enriched_sections (or course_spec as fallback) from the shared state blob.
    Only available after the job has reached COMPLETED status.
    """
    repository = JobRepository(session)
    job = repository.get_job(job_id)
    if job is None:
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

@router.post("/{job_id}/ai", response_model=AIOperationResponse)
async def perform_ai_operation(
    job_id: str,
    payload: AIOperationRequest,
    session: Session = Depends(get_db_session),
) -> AIOperationResponse:
    """Run an AI operation on a specific section's content.

    The operation is applied via Azure OpenAI using the job's rule family context.
    For now this is a structured stub — plug in the LLM call when ready.
    """
    repository = JobRepository(session)
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    t0 = time.monotonic()

    # ── LLM call placeholder ─────────────────────────────────────────────────
    # TODO: wire to Azure OpenAI using the rule pack's style_constraints.
    # The operation-to-prompt mapping should be config-driven in rule_pack_config.
    #
    # result_content = await azure_openai_client.chat(
    #     system_prompt=build_ai_op_prompt(payload.operation, job.course_type),
    #     user_message=payload.content,
    # )
    result_content = f"[{payload.operation.upper()}] {payload.content}"
    # ── End placeholder ──────────────────────────────────────────────────────

    elapsed_ms = int((time.monotonic() - t0) * 1_000)

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

    In production this should return a short-lived Azure Blob SAS URL.
    The current implementation returns the raw blob path — replace with
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
        logger.warning("Could not resolve artifact blob path for job %s: %s", job_id, exc)
        blob_path = None

    if not blob_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DOCX artifact not found. The job may not be complete.",
        )

    # TODO: replace blob_path with a signed URL via BlobRepository.generate_sas_url(blob_path)
    return ArtifactDownloadResponse(
        url=blob_path,
        filename="study_guide.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
