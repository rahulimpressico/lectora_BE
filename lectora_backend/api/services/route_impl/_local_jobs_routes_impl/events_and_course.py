from __future__ import annotations

from .base import *

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
                t = lesson
            else:
                return fallback
        # Strip leading numeric prefixes like "1.0 ", "2.9 ", "3 ", "10.2 " etc.
        t = re.sub(r'^\d+(?:\.\d+)?\s+', '', t).strip()
        return t or fallback

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
            "id": _section_stable_id(sec, i),
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
