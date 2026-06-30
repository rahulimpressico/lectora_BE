from __future__ import annotations

from .base import *

from .helpers_ai_ops import _regenerate_special_section_sync, _set_trace_context_for_job, _rewrite_section_sync, _improve_tone_sync, _summarize_section_sync, _expand_section_sync, _simplify_section_sync, _regenerate_section_sync

def _resolve_and_validate(blob_path: str, label: str) -> str:
    """Resolve *blob_path* to an absolute local path, raising HTTP 422 if missing.

    Uses the shared blob_resolver which:
      1. Checks local _UPLOAD_ROOT cache (fast path).
      2. Downloads from Azure Blob Storage and persists to _UPLOAD_ROOT if
         Azure is configured and the file is not cached locally.
      3. Handles the ``uploaded-documents/`` prefix that Azure browser paths carry.

    Raises
    ------
    HTTPException 422
        When the file cannot be found locally or in Azure, with an actionable
        message telling the user to re-upload the document.
    """
    from lectora_backend.core.blob_resolver import resolve_blob_to_local

    resolved = resolve_blob_to_local(blob_path)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "file_not_found",
                "label": label,
                "blobPath": blob_path,
                "message": (
                    f"{label} could not be located: '{blob_path}'. "
                    "The file may have been uploaded in a previous session that "
                    "has since expired, or it only exists in Azure and could not "
                    "be downloaded. Please re-upload the document and try again."
                ),
            },
        )
    logger.info("[blob] %s resolved: %r → %s", label, blob_path, resolved)
    return str(resolved)

def _paragraphs_to_text(body_paragraphs: list[dict]) -> str:
    """Convert A2 body_paragraphs list to plain readable text."""
    parts: list[str] = []
    for para in body_paragraphs or []:
        ptype = para.get("type", "")
        if ptype == "text":
            parts.append(para.get("content", "").strip())
        elif ptype == "bullet_list":
            items = para.get("items") or []
            parts.append("\n".join(f"• {item}" for item in items))
        elif ptype in ("important_callout", "callout"):
            parts.append(f"Important: {para.get('content', '').strip()}")
        elif ptype in ("heading_3", "heading_4"):
            parts.append(f"### {para.get('content', '').strip()}")
        elif ptype == "knowledge_check":
            lines = [f"Knowledge Check: {para.get('question', '')}"]
            for opt in para.get("options") or []:
                lines.append(f"  {opt}")
            if para.get("explanation"):
                lines.append(f"Answer: {para.get('explanation', '')}")
            parts.append("\n".join(lines))
    return "\n\n".join(p for p in parts if p)

def _section_stable_id(sec: dict, idx: int) -> str:
    """Derive a stable, repeatable frontend ID for an A2 section.

    A2 sets section_id = "" for virtually all sections (it comes from A1 which
    often leaves it blank).  We therefore derive the ID from the section's
    heading (primary) or outline_lesson (fallback), producing a slug that is
    the same every time shared_state is read — unlike a positional "sec_N" ID
    which breaks whenever section order changes.

    Falls back to positional "sec_{idx+1}" only when both heading and
    outline_lesson are absent.
    """
    real_id = (sec.get("section_id") or "").strip()
    if real_id:
        return real_id
    text = (sec.get("heading") or sec.get("outline_lesson") or "").strip()
    if text:
        slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:80]
        return f"h_{slug}"
    return f"sec_{idx + 1}"

def _promote_synthetic_parent(sections: list[dict], synth_sec: dict) -> None:
    """Insert *synth_sec* into *sections* immediately before its first L2 child.

    When a synthetic L1 parent is appended at the end of the list, the next
    call to _inject_missing_lesson_parent_sections processes the L2 children
    before it finds the existing parent, injects a duplicate synthetic parent,
    and the tree ends up with the persisted section having no children.  By
    inserting it in the correct position the duplication is avoided.
    """
    target_lesson = (synth_sec.get("outline_lesson") or synth_sec.get("heading") or "").strip()
    for i, sec in enumerate(sections):
        sec_lesson = (sec.get("outline_lesson") or "").strip()
        if sec_lesson == target_lesson and sec.get("level", 2) != 1:
            sections.insert(i, synth_sec)
            return
    # No L2 child found — append as fallback (e.g., section with no children yet).
    sections.append(synth_sec)

def _find_a2_section(sections: list[dict], section_id: str) -> tuple[dict | None, int]:
    """Locate an A2 output section by the frontend-facing section_id.

    Three-pass lookup so both new (heading-based) and legacy (positional)
    IDs are handled:

    Pass 1 — exact match on the section_id field stored in A2 output.
              Works when A1 assigned a real ID (rare in practice).

    Pass 2 — reconstruct what _section_stable_id / get_course_content assigns
              for each section and compare.  This is the primary path because
              A2 always sets section_id = "" for regular sections, so the
              heading-based slug is what get_course_content now sends to FE.

    Pass 3 — legacy positional fallback: "sec_N" → sections[N-1].
              Handles old FE sessions that cached IDs before this fix.

    Returns (section_dict, index) or (None, -1) when not found.
    """
    # Pass 1: exact section_id field match (A1-derived IDs, very rare)
    for i, sec in enumerate(sections):
        sid = (sec.get("section_id") or "").strip()
        if sid and sid == section_id:
            return sec, i

    # Pass 2: heading-based stable ID match (primary path for all real courses)
    for i, sec in enumerate(sections):
        if _section_stable_id(sec, i) == section_id:
            return sec, i

    # Pass 3: legacy positional fallback for old "sec_N" format IDs
    if section_id.startswith("sec_"):
        try:
            idx = int(section_id[4:]) - 1
            if 0 <= idx < len(sections):
                return sections[idx], idx
        except ValueError:
            pass

    return None, -1

def _persist_section_text(job_id: str, section_id: str, new_content: str) -> None:
    """Persist plain-text content to shared_state for any section (special or regular)."""
    store = get_local_course_job_store()
    job = store.get(job_id) or _recover_job_from_disk(job_id)
    if not job or not job.shared_state_path:
        return
    with open(job.shared_state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)
    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    if section_id == "course-overview":
        a2_output["course_description"] = new_content
    elif section_id == "course-conclusion":
        a2_output["course_conclusion"] = new_content
    else:
        sections: list[dict] = a2_output.get("sections") or []
        target, _ = _find_a2_section(sections, section_id)
        if target is not None:
            target["body_paragraphs"] = [{"type": "text", "content": new_content}]
            target["word_count"] = len(new_content.split())
        a2_output["sections"] = sections
    shared_state["agent_outputs"]["A2"] = a2_output
    with open(job.shared_state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

def _recover_job_from_disk(job_id: str):
    """Find and register a completed job from filesystem when it's absent from the in-memory store.

    This handles the common dev-server-restart case where the in-memory store is empty
    but pipeline/shared_state/{slug}/{job_id}/ still exists on disk.
    Returns the LocalCourseJob if found, or None.
    """
    # Job ID is always a path segment: pipeline/shared_state/{slug}/{job_id}/
    for state_file in (
        *_PIPELINE_COURSES_DIR.glob(f"*/{job_id}/shared_state.json"),
        *_PIPELINE_COURSES_DIR.glob(f"*/{job_id}/state/shared_state.json"),
    ):
        if not state_file.is_file():
            continue
        try:
            with open(state_file, encoding="utf-8") as fh:
                state = json.load(fh)
            artifact_dir = _artifact_dir_from_state_file(state_file)
            docx_candidate = _resolve_study_guide_path(artifact_dir)
            store = get_local_course_job_store()
            return store.register_from_filesystem(
                job_id=job_id,
                course_title=_course_title_from_shared_state(state, artifact_dir.parent.name),
                course_type=_course_type_from_shared_state(state),
                shared_state_path=str(state_file),
                study_guide_path=str(docx_candidate) if docx_candidate else None,
                temp_dir=str(artifact_dir),
            )
        except Exception:
            continue
    return None
