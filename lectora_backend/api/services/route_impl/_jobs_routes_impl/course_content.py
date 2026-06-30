from .base import *

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
            return await get_local_course_content(job_id, course_slug=course_slug)
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
