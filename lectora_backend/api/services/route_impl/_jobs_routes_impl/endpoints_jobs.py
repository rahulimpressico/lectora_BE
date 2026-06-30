from .base import *

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
