from __future__ import annotations

from .base import *

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
