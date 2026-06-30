from .base import *

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
