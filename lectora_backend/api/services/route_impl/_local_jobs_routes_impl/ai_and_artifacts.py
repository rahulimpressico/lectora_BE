from __future__ import annotations

from .base import *

async def run_ai_operation(job_id: str, payload: AIOperationPayload) -> JSONResponse:
    import asyncio

    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
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

async def get_training_outline(job_id: str) -> JSONResponse:
    """Return FE-ready TO + rules from saved llm_to_outline.json (disk or Azure)."""
    from lectora_backend.api.services.to_response_builder import build_fe_to_response_from_llm_outline

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

async def download_artifact(
    job_id: str,
    course_slug: Annotated[str | None, Query(alias="courseSlug")] = None,
    section_order: Annotated[str | None, Query(alias="sectionOrder")] = None,
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

    # Apply FE section order before rebuilding so DOCX matches current editor layout.
    if section_order:
        order_list = [s.strip() for s in section_order.split(",") if s.strip()]
        if order_list:
            _apply_section_order_to_shared_state(state_path, order_list)

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

def _apply_section_order_to_shared_state(
    state_path: "Path", section_order: list[str]
) -> None:
    """Reorder A2 sections in shared_state.json to match the given ID list.

    Works for both heading-based stable IDs (original A2 output, section_id="")
    and UUID-based IDs (FE-created sections where section_id is the UUID).

    When the frontend sends L1-only order (top-level tree nodes), each lesson
    block moves together with all of its L2 subtopics.  Depth-first flat order
    (L1 + children IDs) is also supported.

    Special frontend-only sections (course-overview, etc.) are silently skipped.
    """
    from lectora_backend.api.utils.section_reorder import apply_section_order

    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2", {})
    sections: list[dict] = a2_output.get("sections") or []
    if not sections or not section_order:
        return

    reordered = apply_section_order(sections, section_order, _section_stable_id)

    shared_state["agent_outputs"]["A2"]["sections"] = reordered
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

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
