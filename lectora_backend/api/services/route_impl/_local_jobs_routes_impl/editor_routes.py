from __future__ import annotations

from .base import *

async def reorder_sections(
    job_id: str,
    payload: ReorderSectionsPayload,
) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    state_path = _materialize_shared_state_to_disk(job)
    _apply_section_order_to_shared_state(state_path, payload.section_order)
    return JSONResponse(content={"jobId": job_id, "status": "reordered"})

async def save_section_content(
    job_id: str,
    section_id: str,
    payload: SaveSectionPayload,
) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    state_path = _materialize_shared_state_to_disk(job)

    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    content = (payload.content or "").strip()
    stype = (payload.section_type or "content").strip()

    if stype == "overview":
        a2_output["course_description"] = content

    elif stype == "conclusion":
        a2_output["course_conclusion"] = content

    elif stype == "learning-objectives":
        # Parse "1. text\n2. text" back to plain list
        los: list[str] = []
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\d+\.\s*(.+)$", line)
            los.append(m.group(1).strip() if m else line)
        extracted = shared_state.get("extracted_inputs") or {}
        extracted["learning_objectives"] = los
        shared_state["extracted_inputs"] = extracted

    else:
        # Regular content section — replace body_paragraphs with single plain-text block
        sections: list[dict] = a2_output.get("sections") or []
        target, _ = _find_a2_section(sections, section_id)
        if target is None:
            # May be a synthetic L1 parent injected by _inject_missing_lesson_parent_sections.
            # Promote it into the real sections list so it can be persisted.
            expanded = _inject_missing_lesson_parent_sections(sections)
            synth_sec, _ = _find_a2_section(expanded, section_id)
            if synth_sec:
                _promote_synthetic_parent(sections, synth_sec)
                target, _ = _find_a2_section(sections, section_id)

        if target is None:
            # Brand-new section added by the FE (UUID-based ID not yet in A2).
            # Create it so it persists and appears in the downloaded DOCX.
            # Storing section_id = section_id ensures _section_stable_id returns
            # the same UUID on re-fetch, keeping FE IDs stable after refresh.
            new_sec: dict = {
                "section_id": section_id,
                "heading": (payload.title or "New Subtopic").strip(),
                "outline_lesson": "",
                "level": 2,
                "body_paragraphs": [],
                "word_count": 0,
                "has_knowledge_check": False,
                "is_parent_overview": False,
            }
            sections.append(new_sec)
            target = new_sec

        target["body_paragraphs"] = [{"type": "text", "content": content}]
        target["word_count"] = len(content.split())
        if payload.title and payload.title.strip():
            target["heading"] = payload.title.strip()
        a2_output["sections"] = sections

    shared_state["agent_outputs"]["A2"] = a2_output
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

    return JSONResponse(content={"jobId": job_id, "sectionId": section_id, "status": "saved"})

async def delete_section(
    job_id: str,
    section_id: str,
) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    state_path = _materialize_shared_state_to_disk(job)

    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    sections: list[dict] = a2_output.get("sections") or []

    target, target_idx = _find_a2_section(sections, section_id)
    if target is None:
        # Not in backend — nothing to remove
        return JSONResponse(content={"jobId": job_id, "sectionId": section_id, "status": "not_found"})

    sections.pop(target_idx)
    a2_output["sections"] = sections
    shared_state["agent_outputs"]["A2"] = a2_output

    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

    return JSONResponse(content={"jobId": job_id, "sectionId": section_id, "status": "deleted"})

async def update_course_title(
    job_id: str,
    payload: UpdateCourseTitlePayload,
) -> JSONResponse:
    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    state_path = _materialize_shared_state_to_disk(job)

    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    a2_output["course_title"] = payload.course_title.strip()
    shared_state["agent_outputs"]["A2"] = a2_output

    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

    return JSONResponse(content={"jobId": job_id, "status": "title_updated"})

async def sync_course_content(
    job_id: str,
    payload: SyncCoursePayload,
) -> JSONResponse:
    """Atomically replaces shared_state A2 output with the frontend editor tree.

    Maps:
      course-overview      → a2_output["course_description"]
      course-learning-objectives → extracted_inputs["learning_objectives"]
      course-conclusion    → a2_output["course_conclusion"]
      all other sections   → a2_output["sections"] (depth-first, flat)

    Preserves original A2 metadata (outline_lesson, images, etc.) where the
    section still exists by matching on the stable ID.  Writes atomically via
    .tmp + os.replace so a server crash cannot corrupt shared_state.json.
    """
    import os as _os

    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    state_path = _materialize_shared_state_to_disk(job)

    with open(state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.setdefault("agent_outputs", {}).setdefault("A2", {})
    existing_sections: list[dict] = a2_output.get("sections") or []

    # Pre-build a lookup: stable_id → existing A2 section so we can preserve
    # pipeline-only metadata (outline_lesson, images, maps_to_objectives, etc.)
    existing_by_id: dict[str, dict] = {}
    for i, sec in enumerate(existing_sections):
        sid = _section_stable_id(sec, i)
        existing_by_id[sid] = sec
        real_id = (sec.get("section_id") or "").strip()
        if real_id:
            existing_by_id[real_id] = sec

    new_a2_sections: list[dict] = []
    new_learning_objectives: list[str] = []

    def _body_paragraphs_from_input(
        sec: CourseSectionInput,
        orig: dict | None,
        content: str,
    ) -> list[dict]:
        if sec.paragraphs:
            return list(sec.paragraphs)
        if content:
            return [{"type": "text", "content": content}]
        if orig:
            return list(orig.get("body_paragraphs") or [])
        return []

    def _process(sec: CourseSectionInput, parent_lesson: str = "") -> None:
        stype = (sec.section_type or "content").strip()

        if stype == "overview":
            a2_output["course_description"] = sec.content.strip()
            return

        if stype == "learning-objectives":
            new_learning_objectives.extend(sec.learning_objectives)
            return

        if stype == "conclusion":
            a2_output["course_conclusion"] = sec.content.strip()
            return

        content = sec.content.strip()
        wc = sec.word_count or len(content.split())
        is_parent = sec.level == 1 and bool(sec.children) and not content

        # L2/L3 sections inherit the current L1 lesson; moved subtopics pick up
        # the new parent heading so DOCX grouping matches the editor tree.
        lesson = sec.title.strip() if sec.level == 1 else parent_lesson

        # Base A2 section from FE data
        a2_sec: dict[str, Any] = {
            "section_id": sec.id,
            "heading": sec.title.strip(),
            "outline_lesson": lesson,
            "level": sec.level,
            "body_paragraphs": _body_paragraphs_from_input(
                sec,
                existing_by_id.get(sec.id),
                content,
            ),
            "word_count": wc,
            "has_knowledge_check": sec.has_knowledge_check,
            "is_parent_overview": is_parent,
            "status": "editor_saved",
        }

        # Merge preserved pipeline metadata from the existing A2 section
        orig = existing_by_id.get(sec.id)
        if orig:
            if not a2_sec["body_paragraphs"]:
                a2_sec["body_paragraphs"] = list(orig.get("body_paragraphs") or [])
            a2_sec["images"] = orig.get("images") or []
            a2_sec["maps_to_objectives"] = orig.get("maps_to_objectives") or []
            a2_sec["subtopics"] = orig.get("subtopics") or []
        else:
            a2_sec.setdefault("images", [])

        new_a2_sections.append(a2_sec)

        child_lesson = sec.title.strip() if sec.level == 1 else parent_lesson
        for child in sec.children:
            _process(child, parent_lesson=child_lesson)

    for sec in payload.sections:
        _process(sec)

    a2_output["sections"] = new_a2_sections

    if payload.course_title.strip():
        a2_output["course_title"] = payload.course_title.strip()
        shared_state.setdefault("request", {})["courseTitle"] = payload.course_title.strip()

    if new_learning_objectives:
        shared_state.setdefault("extracted_inputs", {})["learning_objectives"] = (
            new_learning_objectives
        )

    shared_state["agent_outputs"]["A2"] = a2_output

    # Atomic write: write to .tmp then rename over the live file
    tmp_path = state_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)
    _os.replace(str(tmp_path), str(state_path))

    # Recompute meta from the new flat sections
    content_secs = [s for s in new_a2_sections if not s.get("is_parent_overview")]
    total_words = sum(s.get("word_count", 0) for s in content_secs)
    section_count = len(content_secs)
    chapter_count = sum(1 for s in content_secs if s.get("level") == 1)
    read_minutes = max(1, total_words // 200)
    estimated_read = (
        f"{read_minutes} min read"
        if read_minutes < 60
        else f"{read_minutes // 60}h {read_minutes % 60}m"
    )

    logger.info(
        "[sync_course] Synced %d sections for job %s (title=%r)",
        len(new_a2_sections),
        job_id,
        payload.course_title,
    )
    return JSONResponse(content={
        "jobId": job_id,
        "status": "synced",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "meta": {
            "totalWordCount": total_words,
            "sectionCount": section_count,
            "chapterCount": chapter_count,
            "estimatedReadTime": estimated_read,
        },
    })
