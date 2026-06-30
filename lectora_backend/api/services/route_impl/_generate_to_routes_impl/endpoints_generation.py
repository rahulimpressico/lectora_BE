from .base import *
from . import base as _base

_A0_SYNC_TIMEOUT_SEC = _base._A0_SYNC_TIMEOUT_SEC

async def generate_to(
    body: GenerateTORequest,
    wait: bool = Query(
        False,
        description=(
            "If true, block until A0 finishes (may take several minutes; "
            f"times out after {_A0_SYNC_TIMEOUT_SEC}s). Default false = return jobId immediately."
        ),
    ),
):
    """
    Run the Topic Outline generation pipeline (A0 → A1 → S1) on ``blobPath``.

    **Default (recommended for FE):** returns **202** with ``jobId`` immediately.
    Poll ``GET /documents/generate-to/jobs/{jobId}`` every few seconds until
    ``status`` is ``completed`` or ``failed``.

    **Legacy sync:** ``?wait=true`` holds the connection until S1 validation finishes.
    """
    blob_paths = body.effective_blob_paths
    difficulty = (body.difficulty or "intermediate").strip().lower()
    custom_to_prompt = (body.custom_to_prompt or "").strip() or None
    # Prepend any explicit wizard fields so A0 sees structured parameters first.
    explicit_ctx = _build_explicit_context(body)
    if explicit_ctx:
        custom_to_prompt = "\n\n".join(filter(None, [explicit_ctx, custom_to_prompt]))
    # User-provided locked fields: enforced verbatim into llm_to_outline after A0 runs.
    user_title: str | None = (body.course_title or "").strip() or None
    user_description: str | None = (body.course_description or "").strip() or None
    user_los: list[str] | None = list(body.learning_objectives) if body.learning_objectives else None

    # Audience flows as a dedicated parameter to A0 → build_dynamic_to_prompt,
    # not as a text prefix injected into the custom prompt.
    audience = (body.audience or "").strip() or None
    course_type_hint = (body.course_type_hint or "").strip() or None

    # ── Dynamic TO flow params (new) ──────────────────────────────────────────
    duration_hours: float | None = body.duration_hours
    difficulty_level: str | None = (body.difficulty_level or "").strip().lower() or None
    calculated_word_count: int | None = body.calculated_word_count

    # Log the exact values received from the frontend before any transformation.
    logger.debug(
        "[generate-to] RAW request payload: difficulty=%r | difficulty_level=%r | "
        "duration_hours=%r | calculated_word_count=%r | audience=%r | "
        "course_type_hint=%r | learning_objectives_count=%d | "
        "preferred_chapters=%r | lesson_style=%r | "
        "experience_level=%r | tone=%r | depth=%r | "
        "has_description=%s | has_learner_outcomes=%s | has_audience_notes=%s | "
        "has_emphasis=%s | has_avoid=%s | "
        "include_scenarios=%r | include_knowledge_checks=%r | "
        "blob_paths_count=%d | has_custom_prompt=%s | has_to_doc=%s",
        body.difficulty,
        body.difficulty_level,
        body.duration_hours,
        body.calculated_word_count,
        (body.audience or "")[:80] or None,
        body.course_type_hint,
        len(body.learning_objectives) if body.learning_objectives else 0,
        body.preferred_chapters,
        body.lesson_style,
        body.experience_level,
        body.tone,
        body.depth,
        bool(body.course_description),
        bool(body.learner_outcomes),
        bool(body.audience_notes),
        bool(body.emphasis),
        bool(body.avoid),
        body.include_scenarios,
        body.include_knowledge_checks,
        len(blob_paths),
        bool(body.custom_to_prompt),
        bool(body.to_doc_blob_path),
    )

    # When the dynamic flow is active, sync the difficulty string so A0 uses it
    # correctly for rule pack + metrics even if the old `difficulty` field
    # wasn't set by the FE.
    if difficulty_level and not body.difficulty:
        difficulty = difficulty_level

    # A0 accepts DOCX and PDF sources natively — separate and pass both.
    resolved_paths = [_validate_document_path(bp) for bp in blob_paths]
    all_docx = [p for p in resolved_paths if p.suffix.lower() == ".docx"]
    all_pdf = [p for p in resolved_paths if p.suffix.lower() == ".pdf"]

    if not all_docx and not all_pdf:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid DOCX or PDF files found in blobPaths.",
        )

    # Resolve optional user-uploaded TO document (DOCX, PDF, or pre-built JSON)
    to_outline_path: Path | None = None
    if body.to_doc_blob_path:
        to_outline_path = _validate_document_path(body.to_doc_blob_path)
        if to_outline_path.suffix.lower() == ".json":
            logger.info(
                "[generate-to] Pre-built JSON TO detected: %s — A0 will use fast-path loader "
                "(no LLM outline generation); rule classification still runs.",
                to_outline_path.name,
            )
        else:
            logger.info("[generate-to] User-provided TO document: %s", to_outline_path.name)

    output_dir = _LOCAL_SHARED_STATE_DIR
    source_blob = blob_paths[0]
    course_folder = course_folder_from_blob_path(source_blob)

    logger.info(
        "[generate-to] Starting A0 | docx=%d | pdf=%d | difficulty=%s | "
        "duration_hours=%s | calculated_word_count=%s | audience=%s | custom_prompt=%s | course_hint=%s | wait=%s",
        len(all_docx),
        len(all_pdf),
        difficulty,
        duration_hours,
        calculated_word_count,
        bool(audience),
        bool(custom_to_prompt),
        bool(course_type_hint),
        wait,
    )

    def _build_runner(step_logger=None, cancel_event: threading.Event | None = None):
        return _make_a0_runner(
            all_docx,
            all_pdf,
            output_dir,
            difficulty,
            custom_to_prompt=custom_to_prompt,
            course_type_hint=course_type_hint,
            to_outline_doc_path=to_outline_path,
            step_logger=step_logger,
            duration_hours=duration_hours,
            difficulty_level=difficulty_level,
            calculated_word_count=calculated_word_count,
            audience=audience,
            course_description=user_description,
            cancel_event=cancel_event,
        )

    if wait:
        from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

        sync_doc_name = all_docx[0].stem if all_docx else (all_pdf[0].stem if all_pdf else "")
        set_run_context(
            f"sync-{sync_doc_name or 'to'}",
            sync_doc_name or "unknown",
            source_refs=blob_paths,
        )

        a0_runner = _build_runner()
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_to_generation_pipeline,
                    a0_runner=a0_runner,
                    request_body=body,
                    source_doc_path=str(resolved_paths[0]),
                    difficulty=difficulty,
                    source_blob_path=source_blob,
                    user_title=user_title,
                    user_description=user_description,
                    user_los=user_los,
                    step_logger=None,
                ),
                timeout=_A0_SYNC_TIMEOUT_SEC,
            )
            logger.info("[generate-to] TO generation complete (sync) | stages=A0,A1,S1")
            return GenerateTOResponse.model_validate(payload)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    f"TO generation did not finish within {_A0_SYNC_TIMEOUT_SEC}s. "
                    "Retry without wait=true and poll GET /documents/generate-to/jobs/{{jobId}}."
                ),
            ) from exc
        except TOValidationBlockedError as exc:
            validation = getattr(exc, "validation", None)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": f"S1 validation blocked Topic Outline: {exc}",
                    "s1Validation": validation,
                },
            ) from exc
        except ValueError as exc:
            logger.warning("[generate-to] Validation error: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not read document: {exc}",
            ) from exc
        except Exception as exc:
            logger.exception("[generate-to] A0 failed (sync): %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"A0 agent failed: {exc}",
            ) from exc

    # ── Async: return immediately, run A0 in background ─────────────────────
    store = get_generate_to_job_store()
    if not store.acquire_slot():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Server busy — max concurrent A0 jobs reached. "
                f"Active: {store.queue_count()}. Retry in a moment."
            ),
        )

    job = store.create(
        blob_path=source_blob,
        blob_paths=blob_paths,
        course_folder=course_folder,
        difficulty=difficulty,
    )
    poll_url = f"/documents/generate-to/jobs/{job.job_id}"

    reg = register_generate_to(
        job.job_id,
        blob_paths=job.blob_paths,
        course_folder=course_folder,
    )

    def _step_logger(level: str, message: str, stage: str | None = None) -> None:
        store.append_log(job.job_id, level=level, message=message, stage=stage)

    a0_runner = _build_runner(step_logger=_step_logger, cancel_event=reg.cancel_event)

    asyncio.create_task(
        run_a0_job_background(
            job.job_id,
            blob_path=source_blob,
            difficulty=difficulty,
            output_dir=output_dir,
            runner=lambda: _run_to_generation_pipeline(
                a0_runner=a0_runner,
                request_body=body,
                source_doc_path=str(resolved_paths[0]),
                difficulty=difficulty,
                source_blob_path=source_blob,
                user_title=user_title,
                user_description=user_description,
                user_los=user_los,
                step_logger=_step_logger,
            ),
            build_response=lambda payload, _difficulty: payload,
            slot_acquired=True,
            cancel_event=reg.cancel_event,
        )
    )

    accepted = GenerateTOJobAccepted(
        job_id=job.job_id,
        status="processing",
        message="A0 started — pipeline will run A0, then S1, then A1",
        poll_url=poll_url,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=accepted.model_dump(by_alias=True),
    )

async def list_generate_to_jobs() -> JSONResponse:
    store = get_generate_to_job_store()
    jobs = store.list_all()
    return JSONResponse(content=[
        {
            "jobId": j.job_id,
            "status": j.status.value,
            "message": j.message,
            "createdAt": j.created_at,
            "finishedAt": j.finished_at,
            "error": j.error,
            "blobPaths": j.blob_paths,
        }
        for j in jobs
    ])

async def cancel_generate_to_job(job_id: str) -> JSONResponse:
    from lectora_backend.core.job_registry import get_generate_to

    store = get_generate_to_job_store()
    if not store.cancel(job_id, reason="Cancelled by user"):
        job = store.get(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown or expired jobId: {job_id}",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is already {job.status.value}",
        )
    handle = get_generate_to(job_id)
    if handle:
        handle.cancel_event.set()
    return JSONResponse(content={"jobId": job_id, "status": "cancelled"})

async def get_generate_to_job(job_id: str) -> GenerateTOJobPollResponse:
    """Poll the job started by ``POST /documents/generate-to`` (async mode)."""
    store = get_generate_to_job_store()
    job = store.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    if job.status.value == "completed" and job.result:
        return GenerateTOJobPollResponse(
            job_id=job.job_id,
            status="completed",
            message=job.message,
            to=job.result.get("to"),
            rules=job.result.get("rules"),
            to_blob_path=job.result.get("toBlobPath"),
            s1_validation=job.result.get("s1Validation"),
            logs=[log.to_dict() for log in job.logs],
        )

    if job.status.value == "failed":
        return GenerateTOJobPollResponse(
            job_id=job.job_id,
            status="failed",
            message=job.message,
            error=job.error or "A0 failed",
            s1_validation=job.validation,
            logs=[log.to_dict() for log in job.logs],
        )

    if job.status.value == "cancelled":
        return GenerateTOJobPollResponse(
            job_id=job.job_id,
            status="cancelled",
            message=job.message,
            error=job.error or "Cancelled",
            logs=[log.to_dict() for log in job.logs],
        )

    return GenerateTOJobPollResponse(
        job_id=job.job_id,
        status="processing",
        message=job.message,
        logs=[log.to_dict() for log in job.logs],
    )
