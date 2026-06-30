from __future__ import annotations

from .base import *

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
    """Scan pipeline/shared_state/{course_slug}/ for the most recent shared_state.json
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
    the legacy layout ({course_slug}/state/).  Scans pipeline/shared_state/.
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

async def get_job(job_id: str) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
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
